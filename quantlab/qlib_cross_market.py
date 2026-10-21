"""Out-of-universe Qlib transfer: train on BTC/ KODEX200, test elsewhere.

This is historical exploratory research, NOT new untouched temporal holdout.
It uses only source-asset mature labels, an unchanged nine-feature
Microsoft Qlib LGBModel, a fixed p>=.55 long/cash policy, and 20% cap.
The source market's daily calendar MUST EXACTLY equal each target's; never
resample, interpolate or manufacture sessions. US market disabled if
source audit is unavailable. No trading or model promotion.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

from .data import normalize, upbit_daily, naver_daily
from .daily_receipt import _safe_report
from .diagnostics import daily_net_returns, exploratory_familywise_max_t
from .engine import simulate
from .features import FEATURE_COLUMNS, build_features
from .historical_stress import COST_BPS
from .qlib_bridge import _MemoryQlibDataset, _initialize_qlib
from .qlib_failure_diagnostics import outcomes, probabilities_summary
from .qlib_horizon_study import training_masks, horizon_labels
from .settings import Settings

GROUPS={
    "crypto_utc_24h":{
        "source":"KRW-BTC",
        "targets":("KRW-XRP","KRW-ADA"),
        "feed":"upbit",
        "calendar":"UTC crypto sessions seven days a week",
    },
    "krx_local_sessions":{
        "source":"069500",
        "targets":("114800","132030"),
        "feed":"naver",
        "calendar":"KRX weekday trading sessions, no gap filling",
    },
}
START_TRAIN=600
FOLD_DAYS=120
THRESHOLD=.55
BASELINE="buy_hold_20pct"
MA_BASELINE="ma20_80_20pct"
MODEL="source_qlib_055"


def validate_market(frame:pd.DataFrame,feed:str)->pd.DataFrame:
    if feed not in ("upbit","naver"):
        raise ValueError("Unknown exchange calendar")
    if "date" not in frame:
        raise ValueError("Missing market date")
    raw=pd.to_datetime(frame["date"],errors="raise")
    if raw.isna().any() or not raw.is_monotonic_increasing or raw.duplicated().any():
        raise ValueError("Missing, duplicate, or reordered source market dates")
    df=normalize(frame)
    if len(df)<START_TRAIN+60+2:
        raise ValueError("Not enough real trading sessions")
    day_gap=df["date"].diff().dt.days.iloc[1:]
    if feed=="upbit":
        if not day_gap.eq(1).all():
            raise ValueError("Missing exchange UTC crypto candle; never fill")
    else:
        if (df["date"].dt.weekday>=5).any() or day_gap.max()>14:
            raise ValueError("KRX calendar missing long periods or weekend data")
    return df


def match_calendars(source:pd.DataFrame,target:pd.DataFrame)->None:
    if len(source)!=len(target) or not source["date"].equals(target["date"]):
        raise ValueError("Cross-asset target sessions not identical; refuse join")


def source_fit_target_predict(
    source:pd.DataFrame,target:pd.DataFrame,fit_end:int,state_dir:str|Path,
)->tuple[np.ndarray,dict]:
    """Target dates and past features can infer; target labels NEVER fit."""
    from qlib.contrib.model.gbdt import LGBModel
    match_calendars(source,target)
    n=len(source)
    if fit_end<START_TRAIN or fit_end>n-2:
        raise ValueError("Invalid causal fit boundary")
    _initialize_qlib(state_dir)
    f=build_features(source.reset_index(drop=True))
    y=horizon_labels(source.reset_index(drop=True),1,0.)
    train,valid,proof=training_masks(f,y,fit_end,1)
    def segment(mask:np.ndarray)->pd.DataFrame:
        return pd.concat({
            "feature":f.loc[mask,list(FEATURE_COLUMNS)].astype(float),
            "label":y.loc[mask].astype(int).to_frame(name="LABEL0"),
        },axis=1)
    target_features=build_features(target.reset_index(drop=True))
    good=target_features.notna().all(axis=1)
    dataset=_MemoryQlibDataset(
        segment(train),segment(valid),
        target_features.loc[good,list(FEATURE_COLUMNS)].astype(float),
    )
    qlib_model=LGBModel(
        loss="binary",metric="binary_logloss",num_boost_round=70,
        early_stopping_rounds=12,learning_rate=.04,num_leaves=9,
        max_depth=4,min_data_in_leaf=22,lambda_l2=3.,
        feature_fraction=1.,bagging_fraction=1.,seed=42,
        num_threads=1,verbosity=-1,
    )
    qlib_model.fit(dataset,verbose_eval=0)
    predicted=qlib_model.predict(dataset,segment="test")
    result=np.full(n,np.nan,dtype=float)
    result[predicted.index.to_numpy(dtype=int)]=predicted.to_numpy(dtype=float)
    return result,{
        "train_source_only":True,
        "target_labels_used_in_fit":False,
        "trained_source_max_label_open_index":proof["validation_max_label_open_index"],
        "fit_end_exclusive":fit_end,
        "source_feature_count":len(FEATURE_COLUMNS),
        "train_n":int(train.sum()),"validation_n":int(valid.sum()),
    }


def evaluate_transfer(
    datasets:dict[str,dict[str,pd.DataFrame]], *,
    state_dir:str|Path,
    predictor=source_fit_target_predict,
    max_folds:int|None=None,
)->dict:
    if set(datasets)!=set(GROUPS):
        raise ValueError("Expected exactly 2 market families")
    if max_folds is not None and (type(max_folds)!=int or max_folds<1):
        raise ValueError("Invalid fold count")
    model=Settings()
    report={
        "status":"EXPLORATORY_CROSS_MARKET_NO_LIVE_ELIGIBILITY",
        "markets":{},
        "risk_contract":{
            "model":"actual_qlib_contrib_LGBModel_fixed_architecture",
            "feature_columns":list(FEATURE_COLUMNS),
            "fixed_decision_probability":THRESHOLD,
            "maximum_asset_exposure":model.max_position_fraction,
            "fee_bps_per_side":list(COST_BPS),
            "source_labels_mature_before_each_fit":True,
            "target_labels_never_used_for_training":True,
            "test_dates_used_only_for_scoring":True,
            "equal_source_target_market_sessions":True,
            "non_crypto_prices_may_lack_corporate_action_adjustment":True,
            "crypto_vs_equities_returns_not_directly_comparable":True,
            "historical_data_reused_and_not_blind_confirmatory_test":True,
            "no_test_based_model_selection":True,
            "real_orders_enabled":False,
            "automatic_strategy_promotion":False,
        },
    }
    for family,cfg in GROUPS.items():
        expected={cfg["source"],*cfg["targets"]}
        if set(datasets[family])!=expected:
            raise ValueError("Missing/unexpected experiment assets")
        data={symbol:validate_market(frame,cfg["feed"]) for symbol,frame in datasets[family].items()}
        src=data[cfg["source"]]
        for name in cfg["targets"]:
            match_calendars(src,data[name])
        cutoff=len(src)-2
        start_indices=list(range(START_TRAIN,cutoff,FOLD_DAYS))
        report["markets"][family]={
            "source":cfg["source"],"targets":{},"calendar":cfg["calendar"],
            "bars":len(src),"first":src["date"].iloc[0].date().isoformat(),
            "last":src["date"].iloc[-1].date().isoformat(),
        }
        for target in cfg["targets"]:
            df=data[target]
            rows=[]
            folds=[]
            sample_groups=[]
            for i,start in enumerate(start_indices):
                if max_folds is not None and i>=max_folds:
                    break
                end=min(start+FOLD_DAYS,cutoff)
                predictions,proof=predictor(src,df,start,state_dir)
                predictions=np.asarray(predictions,dtype=float)
                if len(predictions)!=len(df) or not np.isfinite(predictions[start:end]).all() or (
                    (predictions[start:end]<0)|(predictions[start:end]>1)
                ).any():
                    raise ValueError("Invalid OOS probabilities")
                if proof["trained_source_max_label_open_index"]>=start or proof["target_labels_used_in_fit"]:
                    raise ValueError("Causal source-only label contract violated")
                ma_fast=df["close"].rolling(20).mean()
                ma_slow=df["close"].rolling(80).mean()
                signal={
                    BASELINE:np.ones(len(df),dtype=float),
                    MA_BASELINE:(ma_fast>ma_slow).astype(float).to_numpy(),
                    MODEL:(predictions>=THRESHOLD).astype(float),
                }
                probs=probabilities_summary(predictions[start:end],outcomes(df,start,end))
                paired={}
                for name,s in signal.items():
                    for cost in COST_BPS:
                        cost_model=replace(model,fee_bps=10.,slippage_bps=float(cost-10))
                        backtest=simulate(df,s,start,end,cost_model,annual_periods=365 if cfg["feed"]=="upbit" else 252)
                        daily=daily_net_returns(df,s,start,end,cost_model)
                        if not np.isclose(np.prod(1.+daily)-1.,backtest["total_return"],atol=1e-9):
                            raise ValueError("Simulator/net daily PnL mismatch")
                        if cost==15:
                            paired[name]=daily
                        rows.append({
                            "fold":i,"name":name,"cost_bps_per_side":cost,
                            "total_return":backtest["total_return"],
                            "max_drawdown":backtest["max_drawdown"],"orders":backtest["orders"],
                            "active_days":backtest["active_days"],
                            "days":backtest["days"],
                        })
                sample_groups.append(paired)
                folds.append({
                    "fold":i,"training_cutoff":src["date"].iloc[start-1].date().isoformat(),
                    "oos_first":df["date"].iloc[start].date().isoformat(),
                    "oos_last":df["date"].iloc[end-1].date().isoformat(),
                    "source_only_training_proof":proof,"probabilities":probs,
                })
            if not folds:
                raise ValueError("No transferred target OOS observations")
            summaries=[]
            for name in (BASELINE,MA_BASELINE,MODEL):
                for cost in COST_BPS:
                    subset=[r for r in rows if r["name"]==name and r["cost_bps_per_side"]==cost]
                    bench={r["fold"]:r for r in rows if r["name"]==BASELINE and r["cost_bps_per_side"]==cost}
                    log_growth=sum(np.log1p(r["total_return"]) for r in subset)
                    summary={
                        "name":name,"cost_bps":cost,"folds":len(subset),
                        "days":sum(r["days"] for r in subset),
                        "orders":sum(r["orders"] for r in subset),
                        "compounded_independent_fold_return":float(np.expm1(log_growth)),
                        "log_excess_vs_same_exposure_buy_hold":float(sum(
                            np.log1p(r["total_return"])-np.log1p(bench[r["fold"]]["total_return"]) for r in subset)),
                        "fold_win_fraction":float(np.mean([
                            r["total_return"]>bench[r["fold"]]["total_return"] for r in subset])),
                        "worst_fold_max_drawdown":float(min(r["max_drawdown"] for r in subset)),
                    }
                    summaries.append(summary)
            report["markets"][family]["targets"][target]={
                "training_source":cfg["source"],
                "folds":folds,"score_rows":rows,"summary":summaries,
                "paired_multiple_testing":exploratory_familywise_max_t(
                    sample_groups,benchmark=BASELINE,candidates=[MA_BASELINE,MODEL],
                    repetitions=300,seed=73,
                ),
            }
    return report


def acquire_snapshot(snapshot_dir:str|Path,*,now:datetime|None=None)->dict:
    """Acquire once; freeze bytes. Data sources public and read-only."""
    root=Path(snapshot_dir)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Refuse to overwrite raw snapshot")
    root.mkdir(parents=True,exist_ok=True)
    provenance={"captured_at_utc":datetime.now(timezone.utc).isoformat(),
                "groups":{},"incomplete_rows_discarded_by_feed":True}
    for family,cfg in GROUPS.items():
        provenance["groups"][family]={"feed":cfg["feed"],"assets":{},"source":cfg["source"],
                                     "target_ids":list(cfg["targets"])}
        for symbol in (cfg["source"],*cfg["targets"]):
            df=(upbit_daily(symbol,3000,now=now) if cfg["feed"]=="upbit"
                else naver_daily(symbol,3000,now=now))
            df=validate_market(df,cfg["feed"])
            name=f"{family}_{symbol.replace('-','_')}.csv"
            raw=df.to_csv(index=False).encode("utf-8")
            (root/name).write_bytes(raw)
            provenance["groups"][family]["assets"][symbol]={
                "filename":name,"sha256":sha256(raw).hexdigest(),
                "records":len(df),"first":df["date"].iloc[0].date().isoformat(),
                "last":df["date"].iloc[-1].date().isoformat(),
            }
        group=provenance["groups"][family]["assets"]
        ss=group[cfg["source"]]
        for target in cfg["targets"]:
            if group[target]["records"]!=ss["records"] or group[target]["first"]!=ss["first"] or group[target]["last"]!=ss["last"]:
                raise ValueError("Different source/target trading calendar: "+family)
    _safe_report(root/"manifest.json",provenance)
    return provenance


def evaluate_snapshot(*,snapshot_dir:str|Path,report_dir:str|Path,state_dir:str|Path,
                      max_folds:int|None=None)->dict:
    root=Path(snapshot_dir)
    meta=json.loads((root/"manifest.json").read_text())
    if set(meta["groups"])!=set(GROUPS):
        raise ValueError("Unregistered source families")
    data={}
    for family,cfg in GROUPS.items():
        evidence=meta["groups"][family]
        if evidence["source"]!=cfg["source"] or evidence["target_ids"]!=list(cfg["targets"]):
            raise ValueError("Source/target definitions changed")
        data[family]={}
        for symbol in (cfg["source"],*cfg["targets"]):
            info=evidence["assets"][symbol]
            expected=f"{family}_{symbol.replace('-','_')}.csv"
            if info["filename"]!=expected:
                raise ValueError("Unexpected dataset filename")
            raw=(root/expected).read_bytes()
            if sha256(raw).hexdigest()!=info["sha256"]:
                raise ValueError("Frozen market CSV SHA-256 mismatch")
            df=validate_market(pd.read_csv(root/expected),cfg["feed"])
            if len(df)!=info["records"] or df["date"].iloc[0].date().isoformat()!=info["first"] or df["date"].iloc[-1].date().isoformat()!=info["last"]:
                raise ValueError("Archived dataset metadata inconsistent")
            data[family][symbol]=df
    target=Path(report_dir)
    if target.exists() and any(target.iterdir()):
        raise ValueError("Refuse overwrite of existing research result")
    result=evaluate_transfer(data,state_dir=state_dir,max_folds=max_folds)
    result["source_provenance"]=meta
    result["evaluated_at_utc"]=datetime.now(timezone.utc).isoformat()
    target.mkdir(parents=True,exist_ok=True)
    _safe_report(target/"results.json",result)
    return result


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="Read-only multi-market external asset Qlib research")
    p.add_argument("--snapshot-dir",default="reports/cross-market-snapshot-20261021")
    p.add_argument("--report-dir",default="reports/cross-market-transfer-20261021")
    p.add_argument("--state-dir",default="state/cross_market_qlib")
    p.add_argument("--acquire",action="store_true",help="Freeze new raw public candles (once)")
    p.add_argument("--max-folds",type=int)
    args=p.parse_args(argv)
    if args.acquire:
        acquire_snapshot(args.snapshot_dir)
    result=evaluate_snapshot(snapshot_dir=args.snapshot_dir,report_dir=args.report_dir,
                             state_dir=args.state_dir,max_folds=args.max_folds)
    print(json.dumps({
        "status":result["status"],
        "report":str(Path(args.report_dir)/"results.json"),
        "markets":{family:{
            target:[{k:r[k] for k in ("name","cost_bps","orders","folds",
                                     "compounded_independent_fold_return",
                                     "log_excess_vs_same_exposure_buy_hold")}
                    for r in blob["summary"] if r["cost_bps"]==15]
            for target,blob in group["targets"].items()
        } for family,group in result["markets"].items()},
        "real_orders_enabled":False,
    },ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

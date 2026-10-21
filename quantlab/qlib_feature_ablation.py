"""Actual Qlib feature ablations and other-asset transfer on identical causal dates.

Pure exploratory historical research: fixed feature families and fixed p>=.55
policy. Source training rows and validation labels mature before fit_end.
Transfer model is trained on OTHER asset history only, never target outcomes.
No training labels, OOS PnL, or model selection flow back into fit.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import numpy as np
import pandas as pd

from .data import normalize
from .daily_receipt import _safe_report
from .diagnostics import daily_net_returns, exploratory_familywise_max_t
from .engine import simulate
from .features import FEATURE_COLUMNS, build_features
from .historical_stress import ASSETS, COST_BPS, verify_input, compare_frozen_archive
from .qlib_bridge import _MemoryQlibDataset, _initialize_qlib
from .qlib_horizon_study import FIRST_TRAIN, FOLD_SIZE, horizon_labels, training_masks
from .qlib_failure_diagnostics import probabilities_summary, outcomes
from .settings import Settings

FAMILIES={
    "full_9":FEATURE_COLUMNS,
    "no_range_volume_7":tuple(x for x in FEATURE_COLUMNS if x not in ("range1","volume_ratio")),
    "returns_only_4":("r1","r3","r5","r10"),
}
TRANSFER="other_asset_full_9"
REFERENCE="buy_hold_20pct"
FIXED_PROB_THRESHOLD=.55


def fit_predict(
    train_frame:pd.DataFrame, target_frame:pd.DataFrame,fit_end:int,feature_set:tuple[str,...],
    state_dir:str|Path,
)->tuple[np.ndarray,dict]:
    from qlib.contrib.model.gbdt import LGBModel
    if not feature_set or len(feature_set)!=len(set(feature_set)) or any(
        key not in FEATURE_COLUMNS for key in feature_set
    ):
        raise ValueError("Unregistered feature family")
    if len(train_frame)!=len(target_frame) or not train_frame["date"].equals(target_frame["date"]):
        raise ValueError("Train/target UTC dates must agree exactly")
    if not FIRST_TRAIN<=fit_end<=len(train_frame)-2:
        raise ValueError("Invalid fit boundary")
    _initialize_qlib(state_dir)
    x=build_features(train_frame.reset_index(drop=True))
    y=horizon_labels(train_frame.reset_index(drop=True),1,0.)
    train,valid,audit=training_masks(x,y,fit_end,1)
    def section(mask):
        values=x.loc[mask,list(feature_set)].astype(float)
        label=y.loc[mask].astype(int).to_frame(name="LABEL0")
        return pd.concat({"feature":values,"label":label},axis=1)
    z=build_features(target_frame.reset_index(drop=True))
    known=z.loc[:,list(FEATURE_COLUMNS)].notna().all(axis=1)
    test=z.loc[known,list(feature_set)].astype(float)
    model=LGBModel(
        loss="binary",metric="binary_logloss",num_boost_round=70,
        early_stopping_rounds=12,learning_rate=.04,num_leaves=9,
        max_depth=4,min_data_in_leaf=22,lambda_l2=3.,
        feature_fraction=1.,bagging_fraction=1.,seed=42,
        num_threads=1,verbosity=-1,
    )
    model.fit(_MemoryQlibDataset(section(train),section(valid),test),verbose_eval=0)
    # Use same adapter, not manual model access (which may change prediction path).
    dataset=_MemoryQlibDataset(section(train),section(valid),test)
    forecast=model.predict(dataset,segment="test")
    result=np.full(len(target_frame),np.nan)
    result[forecast.index.to_numpy(dtype=int)]=forecast.to_numpy(dtype=float)
    audit["trained_on_target_asset"]=(train_frame is target_frame)
    audit["feature_names"]=list(feature_set)
    return result,audit


def run_comparison(
    frames:dict[str,pd.DataFrame],
    *,state_dir:str|Path,
    trainer=fit_predict,
    max_folds:int|None=None,
)->dict:
    if set(frames)!=set(ASSETS):
        raise ValueError("Exactly BTC and ETH required")
    if max_folds is not None and (type(max_folds)!=int or max_folds<1):
        raise ValueError("Invalid fold count")
    data={symbol:verify_input(frame) for symbol,frame in frames.items()}
    if not data[ASSETS[0]]["date"].equals(data[ASSETS[1]]["date"]):
        raise ValueError("Market dates are not fully aligned")
    n=len(data[ASSETS[0]])
    last=n-2
    if last-FIRST_TRAIN<60:
        raise ValueError("Not enough OOS candles")
    result={
        "status":"EXPLORATORY_NO_INDEPENDENT_ALPHA_PROOF",
        "method":"actual_qlib_LGBModel_fixed_1d_binary_ablation_and_cross_asset_transfer",
        "market_days":n,
        "oos_scheme":{"train_initial_days":FIRST_TRAIN,"fold_days":FOLD_SIZE,
                      "no_OOS_tuning":True,"fixed_threshold":FIXED_PROB_THRESHOLD,
                      "cost_bps_per_side":list(COST_BPS),
                      "same_eval_days_across_candidates":True},
        "assets":{},
        "safeguards":{
            "training_has_matured_labels_only":True,
            "transfer_uses_only_other_asset_historical_training":True,
            "all_features_from_completed_utc_candles_only":True,
            "multiple_testing_and_prior_history_selection_not_independent":True,
            "operating_qlib_frozen_checkpoint_unchanged":True,
            "real_orders_enabled":False,
            "automatic_strategy_promotion":False,
        },
    }
    settings=Settings()
    for target in ASSETS:
        other=next(s for s in ASSETS if s!=target)
        df=data[target]
        details=[]
        folds=[]
        paired_groups=[]
        for fold,start in enumerate(range(FIRST_TRAIN,last,FOLD_SIZE)):
            if max_folds is not None and fold>=max_folds:
                break
            end=min(start+FOLD_SIZE,last)
            sigs={REFERENCE:np.ones(n,dtype=float)}
            stats={}
            for name,keys in FAMILIES.items():
                probs,proof=trainer(df,df,start,keys,state_dir)
                if len(probs)!=n or not np.isfinite(probs[start:end]).all():
                    raise ValueError("Invalid target OOS predictions")
                sigs[name]=(np.asarray(probs)>=FIXED_PROB_THRESHOLD).astype(float)
                stats[name]={
                    "maturity":proof,
                    "forecast_quality":probabilities_summary(
                        np.asarray(probs[start:end]),outcomes(df,start,end)
                    ),
                }
            probs,proof=trainer(data[other],df,start,FAMILIES["full_9"],state_dir)
            if len(probs)!=n or not np.isfinite(probs[start:end]).all():
                raise ValueError("Invalid cross-asset OOS predictions")
            sigs[TRANSFER]=(np.asarray(probs)>=FIXED_PROB_THRESHOLD).astype(float)
            stats[TRANSFER]={
                "training_asset":other,
                "target_asset":target,
                "maturity":proof,
                "forecast_quality":probabilities_summary(
                    np.asarray(probs[start:end]),outcomes(df,start,end)
                ),
            }
            paths={}
            for name,sig in sigs.items():
                for cost in COST_BPS:
                    cfg=replace(settings,fee_bps=10.,slippage_bps=float(cost-10))
                    ev=simulate(df,sig,start,end,cfg,annual_periods=365)
                    daily=daily_net_returns(df,sig,start,end,cfg)
                    if not np.isclose(np.prod(1+daily)-1,ev["total_return"],atol=1e-9):
                        raise ValueError("Daily and simulated net return mismatch")
                    if cost==15:
                        paths[name]=daily
                    details.append({
                        "fold":fold,"name":name,"cost_bps_per_side":cost,
                        "days":end-start,"orders":ev["orders"],
                        "total_return":ev["total_return"],
                        "max_drawdown":ev["max_drawdown"],
                    })
            paired_groups.append(paths)
            folds.append({
                "fold":fold,"fit_exclusive":start,"eval_start":start,"eval_end_exclusive":end,
                "training_last_completed_utc":str(df["date"].iloc[start-1].date()),
                "oos_first_utc":str(df["date"].iloc[start].date()),
                "oos_last_utc":str(df["date"].iloc[end-1].date()),
                "candidates":stats,
            })
        if not folds:
            raise ValueError("No OOS fold")
        summary=[]
        for name in (REFERENCE,)+tuple(FAMILIES)+(TRANSFER,):
            for cost in COST_BPS:
                subset=[r for r in details if r["name"]==name and r["cost_bps_per_side"]==cost]
                baseline={r["fold"]:r for r in details if r["name"]==REFERENCE and r["cost_bps_per_side"]==cost}
                summary.append({
                    "name":name,"cost_bps":cost,"folds":len(subset),
                    "oos_days":sum(r["days"] for r in subset),
                    "compounded_isolated_fold_return":float(np.expm1(
                        sum(np.log1p(r["total_return"]) for r in subset))),
                    "log_excess_vs_20pct_buy_hold":float(sum(
                        np.log1p(r["total_return"])-np.log1p(baseline[r["fold"]]["total_return"])
                        for r in subset)),
                    "positive_excess_fold_fraction":float(np.mean([
                        r["total_return"]>baseline[r["fold"]]["total_return"] for r in subset
                    ])),
                    "orders":sum(r["orders"] for r in subset),
                })
        result["assets"][target]={
            "training_assets":{"same_asset":target,"transfer_from_other":other},
            "folds":folds,"score_rows":details,"summary":summary,
            "multiple_testing":exploratory_familywise_max_t(
                paired_groups,benchmark=REFERENCE,candidates=list(FAMILIES)+[TRANSFER],
                repetitions=300,seed=73,
            ),
        }
    return result


def run_saved(*,snapshot_dir:str|Path,output_dir:str|Path,archive:str|Path,
              state_dir:str|Path,max_folds:int|None=None)->dict:
    source=Path(snapshot_dir)
    previous=json.loads((source/"results.json").read_text())
    frames={}
    provenance={}
    for symbol in ASSETS:
        info=previous["datasets"][symbol]
        filename=symbol.replace("-","_")+".csv"
        if info["filename"]!=filename:
            raise ValueError("Unexpected frozen CSV")
        raw=(source/filename).read_bytes()
        if sha256(raw).hexdigest()!=info["sha256"]:
            raise ValueError("Frozen input SHA256 invalid")
        frame=verify_input(pd.read_csv(source/filename))
        verify=compare_frozen_archive(frame,symbol,archive)
        if verify["status"]!="PASS":
            raise ValueError("Archive overlap required")
        provenance[symbol]={"sha256":info["sha256"],"archive":verify}
        frames[symbol]=frame
    target=Path(output_dir)
    if target.exists() and any(target.iterdir()):
        raise ValueError("Research reports are append-only")
    result=run_comparison(frames,state_dir=state_dir,max_folds=max_folds)
    result["source_provenance"]=provenance
    result["created_at_utc"]=datetime.now(timezone.utc).isoformat()
    target.mkdir(parents=True,exist_ok=True)
    _safe_report(target/"results.json",result)
    return result


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser()
    p.add_argument("--snapshot-dir",default="/home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021")
    p.add_argument("--output-dir",default="reports/qlib-feature-study-20261021")
    p.add_argument("--archive",default="/home/hong/apps/FreeQuantLab/state/market_history.db")
    p.add_argument("--state-dir",default="state/qlib_feature_study")
    p.add_argument("--max-folds",type=int)
    args=p.parse_args(argv)
    result=run_saved(snapshot_dir=args.snapshot_dir,output_dir=args.output_dir,
                     archive=args.archive,state_dir=args.state_dir,max_folds=args.max_folds)
    print(json.dumps({
        "status":result["status"],
        "report":str(Path(args.output_dir)/"results.json"),
        "assets":{symbol:[{k:r[k] for k in
            ("name","cost_bps","compounded_isolated_fold_return",
             "log_excess_vs_20pct_buy_hold","orders","folds")}
            for r in body["summary"] if r["cost_bps"]==15]
            for symbol,body in result["assets"].items()},
        "real_orders_enabled":False,
    },ensure_ascii=False))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

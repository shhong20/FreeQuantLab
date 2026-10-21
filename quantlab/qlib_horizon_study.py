"""Exploratory causal Qlib label horizon study (1d/3d/5d, fixed 5d cost-aware).

Predict at completed UTC candle t; enter at t+1 OPEN, optionally hold h
complete OPEN-to-OPEN intervals through t+h+1. A label is eligible for
training only if that latter opening was observable BEFORE the fold cutoff.
Evaluated windows, frozen policy p>=0.55, 20% exposure and transaction
assumptions are identical across horizons. No live trading or promotion.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from hashlib import sha256
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

from .data import normalize
from .features import FEATURE_COLUMNS, build_features
from .qlib_bridge import _MemoryQlibDataset, _initialize_qlib
from .engine import simulate
from .settings import Settings
from .historical_stress import verify_input, compare_frozen_archive, ASSETS, COST_BPS
from .daily_receipt import _safe_report

FIRST_TRAIN=600
FOLD_SIZE=120
# All decision blocks share exactly the same 15-day multiple, so 1,3,5
# day holds do not have shorter partial trades at OOS fold boundaries.
COMMON_MULTIPLE=15
HORIZONS=(
    ("h1_direction",1,0.0),
    ("h3_direction",3,0.0),
    ("h5_direction",5,0.0),
    ("h5_fee_aware_15bp",5,0.003),
)
POLICY_THRESHOLD=.55


def horizon_labels(df:pd.DataFrame, horizon:int, gross_threshold:float=0.0)->pd.Series:
    if horizon not in (1,3,5) or not 0.0<=gross_threshold<=0.01:
        raise ValueError("Unregistered horizon or label cost threshold")
    opening=df["open"].astype(float)
    entry=opening.shift(-1)
    exit_price=opening.shift(-(horizon+1))
    future=exit_price/entry-1
    return (future>gross_threshold).where(future.notna())


def training_masks(
    features:pd.DataFrame, labels:pd.Series, fit_end:int, horizon:int
)->tuple[np.ndarray,np.ndarray,dict]:
    if not 0<fit_end<=len(features) or horizon not in (1,3,5):
        raise ValueError("Invalid fit boundary or horizon")
    # Decision at index j requires open[j+h+1]. At train cutoff
    # only opens with index < fit_end are historically known.
    # Thus j < fit_end - horizon - 1.
    inner=max(100,int(fit_end*.80))
    ix=np.arange(len(features))
    known=features.loc[:,FEATURE_COLUMNS].notna().all(axis=1).to_numpy() & labels.notna().to_numpy()
    train=known & (ix < inner-horizon-1)
    valid=known & (ix>=inner) & (ix<fit_end-horizon-1)
    if np.count_nonzero(train)<80 or np.count_nonzero(valid)<25:
        raise ValueError("Insufficient correctly embargoed Qlib train/validation data")
    if np.any(ix[train]+horizon+1>=inner) or np.any(ix[valid]+horizon+1>=fit_end):
        raise ValueError("Training or validation label maturity violated")
    if labels.iloc[train].nunique()<2 or labels.iloc[valid].nunique()<2:
        raise ValueError("No class diversity in past-only training/validation")
    return train,valid,{
        "train_max_decision_index":int(ix[train].max()),
        "train_max_label_open_index":int(ix[train].max()+horizon+1),
        "inner_fit_cutoff_index":inner,
        "validation_max_decision_index":int(ix[valid].max()),
        "validation_max_label_open_index":int(ix[valid].max()+horizon+1),
        "outer_fit_cutoff_index":fit_end,
        "purged_boundary_bars":horizon+1,
    }


def qlib_horizon_probabilities(
    frame:pd.DataFrame,fit_end:int,horizon:int,gross_threshold:float,
    state_dir:str|Path,
)->tuple[np.ndarray,dict]:
    # Mirrors actual Qlib LGBModel training config; independent of the
    # immutable October operational model's exact checkpoint/protocol.
    from qlib.contrib.model.gbdt import LGBModel
    _initialize_qlib(state_dir)
    df=frame.reset_index(drop=True)
    feats=build_features(df)
    labels=horizon_labels(df,horizon,gross_threshold)
    train,valid,audit=training_masks(feats,labels,fit_end,horizon)

    def section(mask):
        x=feats.loc[mask,FEATURE_COLUMNS].astype(float)
        y=labels.loc[mask].astype(int).to_frame(name="LABEL0")
        return pd.concat({"feature":x,"label":y},axis=1)
    usable=feats.loc[feats.notna().all(axis=1),FEATURE_COLUMNS].astype(float)
    dataset=_MemoryQlibDataset(section(train),section(valid),usable)
    model=LGBModel(
        loss="binary",metric="binary_logloss",num_boost_round=70,
        early_stopping_rounds=12,learning_rate=.04,num_leaves=9,
        max_depth=4,min_data_in_leaf=22,lambda_l2=3.,
        feature_fraction=1.,bagging_fraction=1.,seed=42,
        num_threads=1,verbosity=-1,
    )
    model.fit(dataset,verbose_eval=0)
    output=model.predict(dataset,segment="test")
    prob=np.full(len(df),np.nan)
    prob[output.index.to_numpy(dtype=int)]=output.to_numpy(dtype=float)
    return prob,audit


def blocked_exposure(
    probabilities:np.ndarray,start:int,end:int,horizon:int,
    *,threshold:float=POLICY_THRESHOLD,
)->np.ndarray:
    if horizon not in (1,3,5) or not 0<=start<end<=len(probabilities) or (end-start)%horizon:
        raise ValueError("Only complete non-overlapping holding blocks allowed")
    p=np.asarray(probabilities,dtype=float)
    out=np.zeros(len(p),dtype=float)
    for decision in range(start,end,horizon):
        value=p[decision]
        if not np.isfinite(value) or not 0<=value<=1:
            raise ValueError("Invalid Qlib prediction on trade decision date")
        out[decision:decision+horizon]=float(value>=threshold)
    return out


def run_horizons(
    df:pd.DataFrame,
    *,
    state_dir:str|Path,
    predictor=qlib_horizon_probabilities,
    max_folds:int|None=None,
)->dict:
    frame=verify_input(df)
    if max_folds is not None and (type(max_folds) is not int or max_folds<1):
        raise ValueError("Invalid fold bound")
    n=len(frame)
    last=(n-2-FIRST_TRAIN)//COMMON_MULTIPLE*COMMON_MULTIPLE+FIRST_TRAIN
    if last-FIRST_TRAIN<FOLD_SIZE:
        raise ValueError("Not enough shared OOS evaluation days")
    cfg=Settings()
    records=[]
    bounds=[]
    for fold,start in enumerate(range(FIRST_TRAIN,last,FOLD_SIZE)):
        if max_folds is not None and fold>=max_folds:
            break
        end=min(start+FOLD_SIZE,last)
        if (end-start)%COMMON_MULTIPLE:
            raise ValueError("Fold does not preserve all holding horizons")
        tests={"buy_hold_20pct":np.ones(n)}
        audits={}
        # Fixed, fully preregistered candidate family; do not choose
        # thresholds on heldout test outcomes or change exposure for a winner.
        for name,horizon,gross_threshold in HORIZONS:
            probs, audit=predictor(frame,start,horizon,gross_threshold,state_dir)
            if len(probs)!=n:
                raise ValueError("Model returned mismatched feature dates")
            tests[name]=blocked_exposure(probs,start,end,horizon)
            audits[name]=audit
        for name,signal in tests.items():
            for cost in COST_BPS:
                settings=replace(cfg,fee_bps=10.,slippage_bps=float(cost-10))
                result=simulate(frame,signal,start,end,settings,annual_periods=365)
                records.append({
                    "fold":fold,"name":name,"cost_bps_per_side":cost,
                    "first_decision_utc":frame["date"].iloc[start].date().isoformat(),
                    "last_decision_utc":frame["date"].iloc[end-1].date().isoformat(),
                    "days":end-start,
                    "orders":result["orders"],
                    "active_days":result["active_days"],
                    "total_return":result["total_return"],
                    "max_drawdown":result["max_drawdown"],
                })
        bounds.append({
            "fold":fold,"fit_end_exclusive":start,"evaluation_end_exclusive":end,
            "training_closed_through":frame["date"].iloc[start-1].date().isoformat(),
            "first_decision_utc":frame["date"].iloc[start].date().isoformat(),
            "last_decision_utc":frame["date"].iloc[end-1].date().isoformat(),
            "label_maturity_evidence":audits,
        })
    if not bounds:
        raise ValueError("No evaluation folds")
    summary=[]
    for name in ("buy_hold_20pct",)+tuple(x[0] for x in HORIZONS):
        for cost in COST_BPS:
            subset=[x for x in records if x["name"]==name and x["cost_bps_per_side"]==cost]
            benchmark={x["fold"]:x for x in records
                       if x["name"]=="buy_hold_20pct" and x["cost_bps_per_side"]==cost}
            summary.append({
                "name":name,"cost_bps":cost,"folds":len(subset),
                "oos_days":sum(x["days"] for x in subset),
                "compounded_isolated_fold_return":float(np.expm1(sum(np.log1p(
                    x["total_return"]) for x in subset))),
                "log_excess_vs_buy_hold":float(sum(
                    np.log1p(x["total_return"])-np.log1p(benchmark[x["fold"]]["total_return"])
                    for x in subset
                )),
                "winning_fold_fraction":float(np.mean([
                    x["total_return"]>benchmark[x["fold"]]["total_return"] for x in subset
                ])),
                "orders":sum(x["orders"] for x in subset),
                "worst_fold_return":float(min(x["total_return"] for x in subset)),
            })
    return {
        "status":"EXPLORATORY_LABEL_RESEARCH_ONLY",
        "engine":"actual_qlib_LGBModel_retrained_per_fold",
        "source_period":{"first":frame["date"].iloc[0].date().isoformat(),
                         "last":frame["date"].iloc[-1].date().isoformat(),
                         "bars":n},
        "design":{
            "candidates":[{"name":x,"horizon":h,"training_threshold":t}
                          for x,h,t in HORIZONS],
            "fixed_entry_probability":POLICY_THRESHOLD,
            "label_price":"open[t+1+h]/open[t+1]-1",
            "training_uses_only_labels_matured_before_fold_boundary":True,
            "fold_days_multiple":COMMON_MULTIPLE,
            "same_nonoverlapping_dates_and_max_exposure":True,
            "signal_held_without_rebalancing_until_next_horizon":True,
            "fee_aware_break_even_train_threshold":"30bp asset return for 15bp each-way trade assumption",
            "cost_bps_per_side":list(COST_BPS),
            "train_initial_bars":FIRST_TRAIN,
            "evaluation_bars_per_fold":FOLD_SIZE,
            "excluded_end_bars_due_to_incomplete_shared_blocks":n-2-last,
            "no_test_based_policy_selection":True,
            "historical_data_previously_inspected":True,
            "not_original_frozen_operational_qlib_model":True,
            "not_new_clean_holdout":True,
        },
        "bounds":bounds,"folds":records,"summary":summary,
        "automatic_strategy_promotion":False,"real_orders_enabled":False,
    }


def run_saved(
    *,snapshot_dir:str|Path,output_dir:str|Path,
    archive:str|Path,state_dir:str|Path,max_folds:int|None=None,
)->dict:
    source=Path(snapshot_dir)
    meta=json.loads((source/"results.json").read_text())
    output=Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Historical report destination must be empty")
    result={
        "kind":"qlib_multi_horizon_comparison_v1",
        "created_at_utc":datetime.now(timezone.utc).isoformat(),
        "assets":{},"automatic_strategy_promotion":False,"real_orders_enabled":False,
    }
    for symbol in ASSETS:
        obj=meta["datasets"][symbol]
        name=symbol.replace("-","_")+".csv"
        if obj["filename"]!=name:
            raise ValueError("Unexpected historical source filename")
        raw=(source/name).read_bytes()
        if sha256(raw).hexdigest()!=obj["sha256"]:
            raise ValueError("Historical market source SHA256 mismatched")
        frame=verify_input(pd.read_csv(source/name))
        overlap=compare_frozen_archive(frame,symbol,archive)
        if overlap["status"]!="PASS":
            raise ValueError("Archived recent 800 candles do not match")
        result["assets"][symbol]={
            "original_csv_sha256":obj["sha256"],
            "frozen_archive_overlap":overlap,
            "comparison":run_horizons(frame,state_dir=state_dir,max_folds=max_folds),
        }
    output.mkdir(parents=True,exist_ok=True)
    _safe_report(output/"results.json",result)
    return result


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="Causal Qlib multi-horizon research, NO REAL TRADING")
    p.add_argument("--snapshot-dir",default="/home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021")
    p.add_argument("--output-dir",default="reports/horizon-comparison-20261021")
    p.add_argument("--archive",default="/home/hong/apps/FreeQuantLab/state/market_history.db")
    p.add_argument("--state-dir",default="state/qlib_multi_horizon")
    p.add_argument("--max-folds",type=int)
    args=p.parse_args(argv)
    report=run_saved(snapshot_dir=args.snapshot_dir,output_dir=args.output_dir,
                     archive=args.archive,state_dir=args.state_dir,max_folds=args.max_folds)
    print(json.dumps({
        "status":report["kind"],
        "report":str(Path(args.output_dir)/"results.json"),
        "summary":{symbol:[
            {k:s[k] for k in ("name","cost_bps","folds","orders",
                              "compounded_isolated_fold_return","log_excess_vs_buy_hold")}
            for s in blob["comparison"]["summary"] if s["cost_bps"]==15
        ] for symbol,blob in report["assets"].items()},
        "real_orders_enabled":False,
    },ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

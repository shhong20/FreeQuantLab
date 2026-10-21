"""Read-only failure attribution for already-fixed Qlib walk-forward policies.

Probability diagnostics deliberately use *future* open-to-open labels ONLY as
scoring outcomes; they never feed training, feature construction, threshold
selection, execution, or strategy promotion. Regimes are trailing/causal tags,
and paired excess is logged against the exact same-day 20%-exposure benchmark.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from .diagnostics import trailing_regimes

CALIBRATION_EDGES = (0.0, 0.4, 0.45, 0.5, 0.55, 0.6, 1.0)


def outcomes(df: pd.DataFrame, start: int, end: int) -> np.ndarray:
    if not 0 <= start < end <= len(df)-2:
        raise ValueError("Only decisions with both future opens known can be scored")
    opening = df["open"].to_numpy(dtype=float)
    p1, p2 = opening[start+1:end+1],opening[start+2:end+2]
    if (not np.isfinite(p1).all() or not np.isfinite(p2).all()
        or (p1<=0).any() or (p2<=0).any()):
        raise ValueError("Invalid future observed opens")
    return p2/p1-1


def probabilities_summary(probabilities: np.ndarray, realized_returns: np.ndarray) -> dict:
    p=np.asarray(probabilities,dtype=float)
    r=np.asarray(realized_returns,dtype=float)
    if (p.ndim!=1 or r.ndim!=1 or len(p)!=len(r) or not len(p)
        or not np.isfinite(p).all() or np.any((p<0)|(p>1))
        or not np.isfinite(r).all() or np.any(r<=-1)):
        raise ValueError("Prediction/label samples must be finite aligned valid vectors")
    y=(r>0).astype(int)  # forward_label uses STRICT >0, ties are negative
    tiny=1e-12
    clipped=np.clip(p,tiny,1-tiny)
    buckets=[]
    for i,(left,right) in enumerate(zip(CALIBRATION_EDGES,CALIBRATION_EDGES[1:])):
        mask=(p>=left)&((p<=right) if i==len(CALIBRATION_EDGES)-2 else (p<right))
        buckets.append({
            "lower_inclusive":left, "upper_inclusive_only_if_last":right,
            "observations":int(mask.sum()),
            "mean_probability":float(p[mask].mean()) if mask.any() else None,
            "empirical_up_fraction":float(y[mask].mean()) if mask.any() else None,
            "mean_forward_open_return":float(r[mask].mean()) if mask.any() else None,
        })
    return {
        "observations":len(p),
        "up_fraction":float(y.mean()),
        "predicted_up_fraction_at_050":float((p>=.50).mean()),
        "predicted_up_fraction_at_055":float((p>=.55).mean()),
        "probability_mean":float(p.mean()),
        "probability_std":float(p.std()),
        "brier":float(np.mean((p-y)**2)),
        "brier_constant_050":float(np.mean((.5-y)**2)),
        "log_loss":float(-np.mean(y*np.log(clipped)+(1-y)*np.log1p(-clipped))),
        "accuracy_at_050":float(((p>=.5)==y).mean()),
        "accuracy_at_055":float(((p>=.55)==y).mean()),
        "always_up_accuracy":float(y.mean()),
        "roc_auc":float(roc_auc_score(y,p)) if len(set(y))==2 else None,
        "calibration_bins":buckets,
        "scored_outcome_is_next_open_to_following_open":True,
        "heldout_labels_used_only_for_ex_post_scoring":True,
        "is_model_recalibrated":False,
        "is_selection_metric":False,
    }


def paired_regime_excess(
    df: pd.DataFrame, start: int, end: int,
    paths: dict[str,np.ndarray], benchmark: str,
    *,
    regimes: pd.DataFrame | None=None,
) -> dict:
    if benchmark not in paths:
        raise ValueError("Reference baseline missing")
    table=regimes if regimes is not None else trailing_regimes(df)
    if len(table)!=len(df) or end>len(df):
        raise ValueError("Regimes and daily results do not align")
    reference=np.asarray(paths[benchmark],dtype=float)
    n=end-start
    if len(reference)!=n or not np.isfinite(reference).all() or (reference<=-1).any():
        raise ValueError("Invalid reference daily path")
    out={}
    for label,path in paths.items():
        if label==benchmark:
            continue
        daily=np.asarray(path,dtype=float)
        if len(daily)!=n or not np.isfinite(daily).all() or (daily<=-1).any():
            raise ValueError("Invalid strategy daily path")
        delta=np.log1p(daily)-np.log1p(reference)
        out[label]={}
        for axis in ("direction","volatility"):
            tags=table.iloc[start:end][axis].to_numpy()
            agg={}
            for name in sorted(set(tags)):
                mask=tags==name
                agg[str(name)]={
                    "days":int(mask.sum()),
                    "sum_log_excess_vs_buy_hold":float(delta[mask].sum()),
                    "positive_excess_daily_fraction":float((delta[mask]>0).mean()),
                    "mean_daily_log_excess":float(delta[mask].mean()),
                }
            if not np.isclose(sum(d["sum_log_excess_vs_buy_hold"] for d in agg.values()),
                              float(delta.sum()),atol=1e-10):
                raise ValueError("Regime log contribution does not reconstruct total")
            out[label][axis]=agg
        out[label]["total_log_excess"]=float(delta.sum())
    return out


def aggregate_regimes(folds: list[dict]) -> dict:
    accum={}
    for fold in folds:
        for strategy,details in fold.items():
            dest=accum.setdefault(strategy,{})
            for axis in ("direction","volatility"):
                axis_out=dest.setdefault(axis,{})
                for label,item in details[axis].items():
                    row=axis_out.setdefault(label,{"days":0,"sum_log_excess_vs_buy_hold":0.0})
                    row["days"]+=item["days"]
                    row["sum_log_excess_vs_buy_hold"]+=item["sum_log_excess_vs_buy_hold"]
    for strat,axes in accum.items():
        for axis,rows in axes.items():
            for label,row in rows.items():
                row["mean_daily_log_excess"]=row["sum_log_excess_vs_buy_hold"]/row["days"]
    return accum

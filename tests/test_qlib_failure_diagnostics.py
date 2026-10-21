"""Read-only Qlib diagnostic controls; deterministic and no market API calls."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantlab.qlib_failure_diagnostics import (
    outcomes,probabilities_summary,paired_regime_excess,aggregate_regimes,
)
from quantlab.data import demo_daily


def sample(n=850):
    d=demo_daily(n)
    d["date"]=pd.date_range("2021-01-01",periods=n)
    return d


def test_prediction_scores_are_strictly_next_open_to_following_open():
    frame=sample()
    frame.loc[601,"open"]=100
    frame.loc[602,"open"]=103
    frame.loc[603,"open"]=100
    realized=outcomes(frame,600,602)
    assert realized[0]==pytest.approx(.03)
    assert realized[1]==pytest.approx(100/103-1)
    result=probabilities_summary(np.array([.7,.3]),realized)
    assert result["accuracy_at_050"]==1
    assert result["roc_auc"]==1
    assert result["brier"]==pytest.approx(.09)
    assert sum(x["observations"] for x in result["calibration_bins"])==2
    assert not result["is_model_recalibrated"]


def test_flat_return_is_negative_class_and_missing_last_two_future_opens_rejected():
    d=sample()
    d.loc[601,"open"]=100
    d.loc[602,"open"]=100
    assert outcomes(d,600,601)[0]==0
    assert probabilities_summary(np.array([.3]),np.array([0.]))["up_fraction"]==0.
    assert probabilities_summary(np.array([.6]),np.array([0.]))["roc_auc"] is None
    with pytest.raises(ValueError,match="future opens"):
        outcomes(d,849,850)


def test_probability_error_and_bins_boundaries():
    p=np.array([0,.4,.45,.50,.55,.6,1.])
    o=np.array([-.01,.01,-.01,.01,-.02,.02,.02])
    score=probabilities_summary(p,o)
    assert [b["observations"] for b in score["calibration_bins"]]==[1,1,1,1,1,2]
    assert score["brier_constant_050"]==.25
    assert np.isfinite(score["log_loss"])
    for bad in (np.array([np.nan,0]),np.array([1.2,0])):
        with pytest.raises(ValueError):
            probabilities_summary(bad,np.array([0.,0.]))


def test_paired_regimes_aligned_reconstruct_excess_without_peeking():
    d=sample()
    a=np.array([.01,-.02,.03,.0,.04])
    b=np.array([.0,.01,-.01,.01,.01])
    result=paired_regime_excess(
        d,600,605,{"buy_hold_20pct":b,"qlib_055":a},"buy_hold_20pct",
    )
    expected=float((np.log1p(a)-np.log1p(b)).sum())
    assert result["qlib_055"]["total_log_excess"]==pytest.approx(expected)
    for axis in ("direction","volatility"):
        assert sum(t["sum_log_excess_vs_buy_hold"]
                   for t in result["qlib_055"][axis].values())==pytest.approx(expected)
    summary=aggregate_regimes([result,result])
    for axis in ("direction","volatility"):
        assert sum(t["sum_log_excess_vs_buy_hold"]
                   for t in summary["qlib_055"][axis].values())==pytest.approx(2*expected)
    with pytest.raises(ValueError):
        paired_regime_excess(d,600,605,{"buy_hold_20pct":b,"qlib_055":a[:-1]},
                             "buy_hold_20pct")

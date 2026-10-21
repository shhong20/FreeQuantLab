"""Offline Qlib historical stress validation: no live model or internet needed."""
from __future__ import annotations

import json
from hashlib import sha256
import numpy as np
import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.qlib_historical_stress import (
    fixed_policies, analyze, evaluate_saved_snapshots, BASELINE, HYSTERESIS,
)


def sample(n=850):
    df=demo_daily(n)
    df["date"]=pd.date_range("2021-01-01", periods=n)
    return df


def fake_model(frame,fit_end,state_dir):
    # This fake has no access to actual future labels; it reproduces
    # a predeclared probability pattern to isolate and test execution logic.
    p=np.full(len(frame),.52,dtype=float)
    p[fit_end:]=np.where(np.arange(len(frame)-fit_end)%2==0,.58,.42)
    return p


def test_hysteresis_policy_is_causal_and_uses_only_past_predictions():
    p=np.array([.01,.62,.55,.44,.63,.45,.30,.98,.20])
    v=fixed_policies(p,1,len(p))
    assert v[HYSTERESIS].tolist()==[0,1,1,0,1,1,0,1,0]
    assert v[BASELINE].tolist()==[0,1,1,0,1,0,0,1,0]
    changed=p.copy()
    changed[-1]=.99
    before=fixed_policies(p,1,len(p))[HYSTERESIS]
    after=fixed_policies(changed,1,len(p))[HYSTERESIS]
    np.testing.assert_equal(before[:-1],after[:-1])


def test_bad_probabilities_fail_closed():
    p=np.array([.9,float("nan"),.8])
    with pytest.raises(ValueError,match="invalid"):
        fixed_policies(p,0,3)
    with pytest.raises(ValueError):
        fixed_policies(np.array([1.1,.7]),0,2)


def test_fixed_two_policies_and_trade_costs_without_selection():
    result=analyze(sample(),state_dir="unused",max_folds=2,
                   predictor=fake_model,block_repetitions=100)
    assert result["evaluation"]["folds"]==2
    assert result["engine"]=="qlib.contrib.model.gbdt.LGBModel"
    assert result["evaluation"]["causal_refits_only"]
    assert not result["real_orders_enabled"]
    base=[s for s in result["summary"] if s["name"]==BASELINE and s["cost_bps"]==15][0]
    conservative=[s for s in result["summary"] if s["name"]==HYSTERESIS and s["cost_bps"]==15][0]
    assert conservative["simulated_orders"] < base["simulated_orders"]
    assert conservative["simulated_orders"]==0
    assert result["exploratory_multiple_testing"]["eligible_for_automatic_promotion"] is False
    folds=result["training_boundaries"]
    assert folds[0]["decision_last"]<folds[1]["decision_first"]
    assert folds[0]["last_training_label_decision_max_index"] < folds[0]["fit_end_exclusive_index"]


def test_bad_snapshot_hash_refuses_output_before_model_call(tmp_path):
    source=tmp_path/"inputs"
    source.mkdir()
    (source/"KRW_BTC.csv").write_text("tampered")
    (source/"results.json").write_text(json.dumps({"datasets":{
        "KRW-BTC":{"filename":"KRW_BTC.csv","sha256":"0"*64}
    }}))
    target=tmp_path/"out"
    with pytest.raises(ValueError,match="hash"):
        evaluate_saved_snapshots(snapshot_dir=source,report_dir=target,
            archive=tmp_path/"archive.db",state_dir=tmp_path/"qlib",
            predictor=lambda *args:pytest.fail("No training when data hash wrong"))
    assert not target.exists()


def test_real_qlib_training_ignores_future_prices_and_labels(tmp_path):
    pytest.importorskip("qlib")
    from quantlab.qlib_bridge import qlib_probabilities
    original=sample(850)
    mutated=original.copy()
    # Entire OOS prefix is altered, including prices and prospective labels.
    # The model fitted at 600 must be invariant to all of these changes.
    mutated.loc[600:,["open","high","low","close"]] *= 1.12
    mutated.loc[600:,"volume"] *= 1.20
    raw=qlib_probabilities(original,600,tmp_path/"qlib_model_check")
    changed=qlib_probabilities(mutated,600,tmp_path/"qlib_model_check")
    np.testing.assert_allclose(raw[590:600],changed[590:600],rtol=0,atol=1e-12)
    assert np.isfinite(raw[600:720]).all()

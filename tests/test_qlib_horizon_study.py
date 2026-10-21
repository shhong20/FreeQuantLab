"""Multi-horizon target/hold evaluation leakage and fair-comparison contracts."""
import numpy as np
import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.features import build_features
from quantlab.qlib_horizon_study import (
    horizon_labels,training_masks,blocked_exposure,run_horizons,HORIZONS,
)


def candles(n=850):
    df=demo_daily(n)
    df["date"]=pd.date_range("2021-01-01",periods=n)
    return df


def test_multihorizon_returns_use_correct_future_opens_and_tail_is_missing():
    df=candles()
    p=df["open"].astype(float)
    for h in (1,3,5):
        label=horizon_labels(df,h)
        expected=(p.iloc[10+h+1]/p.iloc[11]-1)>0
        assert label.iloc[10]==bool(expected)
        assert label.iloc[-h-1:].isna().all()
        assert label.iloc[-h-2] in (True,False)
    fee_aware=horizon_labels(df,5,.003)
    raw=horizon_labels(df,5)
    assert (fee_aware.eq(True).astype(int)<=raw.eq(True).astype(int)).all()


def test_training_labels_mature_before_both_inner_and_outer_cutoffs():
    df=candles()
    feats=build_features(df)
    for h in (1,3,5):
        mask,valid,ev=training_masks(feats,horizon_labels(df,h),600,h)
        assert ev["train_max_label_open_index"] < ev["inner_fit_cutoff_index"]
        assert ev["validation_max_label_open_index"] < 600
        assert mask[600:].sum()==valid[600:].sum()==0
        assert np.flatnonzero(valid).max() == 600-h-2
        assert not (mask&valid).any()


def test_holding_blocks_are_contiguous_without_future_signal_mixing():
    p=np.zeros(850)
    p[600]=.8;p[603]=.3;p[606]=.7;p[609]=.3;p[612]=.8
    for h in (3,5):
        sig=blocked_exposure(p,600,615,h)
        assert sig[:600].sum()==sig[615:].sum()==0
        for t in range(600,615,h):
            assert np.unique(sig[t:t+h]).size==1
            assert sig[t]==int(p[t]>=.55)
    with pytest.raises(ValueError):
        blocked_exposure(p,600,611,3)
    with pytest.raises(ValueError):
        blocked_exposure(np.full(850,np.nan),600,615,5)


def fake_model(df,fit_end,horizon,threshold,state_dir):
    # probability depends only on a fixed sequence, not OOS price outcomes.
    p=np.full(len(df),.7)
    p[fit_end::2]=.4
    return p,{
        "train_max_label_open_index":fit_end-10,
        "validation_max_label_open_index":fit_end-1,
        "outer_fit_cutoff_index":fit_end,
        "purged_boundary_bars":horizon+1,
    }


def test_identical_OOS_dates_strategy_set_fixed_and_cost_sensitivity():
    result=run_horizons(candles(),state_dir="unused",predictor=fake_model,max_folds=2)
    assert result["status"]=="EXPLORATORY_LABEL_RESEARCH_ONLY"
    assert not result["real_orders_enabled"]
    assert not result["automatic_strategy_promotion"]
    assert result["design"]["no_test_based_policy_selection"]
    assert len(result["bounds"])==2
    assert len(result["folds"])==2*5*3
    assert {x["days"] for x in result["folds"]}=={120}
    all_names={"buy_hold_20pct"}|{x[0] for x in HORIZONS}
    assert {r["name"] for r in result["folds"]}==all_names
    for fold in (0,1):
        dates={(r["first_decision_utc"],r["last_decision_utc"])
               for r in result["folds"] if r["fold"]==fold}
        assert len(dates)==1
    for name in all_names:
        sums={s["cost_bps"]:s for s in result["summary"] if s["name"]==name}
        assert sums[15]["compounded_isolated_fold_return"]>=sums[30]["compounded_isolated_fold_return"]
        assert sums[30]["compounded_isolated_fold_return"]>=sums[60]["compounded_isolated_fold_return"]


def test_actual_qlib_training_prices_after_boundary_do_not_retroactively_affect_fit(tmp_path):
    pytest.importorskip("qlib")
    from quantlab.qlib_horizon_study import qlib_horizon_probabilities
    df=candles()
    altered=df.copy()
    altered.loc[600:,["open","close","high","low"]]*=1.15
    altered.loc[600:,"volume"]*=1.1
    first,a=qlib_horizon_probabilities(df,600,5,.003,tmp_path/"model")
    second,b=qlib_horizon_probabilities(altered,600,5,.003,tmp_path/"model")
    np.testing.assert_allclose(first[580:600],second[580:600],atol=1e-12,rtol=0)
    assert a==b
    assert a["validation_max_label_open_index"]<600


def test_h1_qlib_probabilities_reproduce_existing_bridge_model(tmp_path):
    pytest.importorskip('qlib')
    from quantlab.qlib_bridge import qlib_probabilities
    from quantlab.qlib_horizon_study import qlib_horizon_probabilities
    frame=candles()
    old=qlib_probabilities(frame,600,tmp_path/'qlib_both')
    new,audit=qlib_horizon_probabilities(frame,600,1,0.0,tmp_path/'qlib_both')
    np.testing.assert_allclose(new[580:730],old[580:730],atol=1e-12,rtol=0)
    assert audit['validation_max_label_open_index']<600


def test_all_horizons_share_partial_last_fold_and_no_missing_future_open():
    result=run_horizons(candles(3000),state_dir='unused',predictor=fake_model)
    assert result['design']['excluded_end_bars_due_to_incomplete_shared_blocks']==13
    assert len(result['bounds'])==20
    assert result['bounds'][-1]['evaluation_end_exclusive']==2985
    last=[x for x in result['folds'] if x['fold']==19]
    assert {x['days'] for x in last}=={105}
    assert len({(x['first_decision_utc'],x['last_decision_utc']) for x in last})==1
    assert len(last)==len({x['name'] for x in last})*3

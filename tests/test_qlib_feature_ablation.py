"""Unit and genuine-Qlib causal ablation & cross-asset control tests."""
import numpy as np
import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.features import FEATURE_COLUMNS
from quantlab.qlib_feature_ablation import (
    run_comparison,fit_predict,FAMILIES,TRANSFER,REFERENCE,
)


def frame(n=855):
    out=demo_daily(n)
    out["date"]=pd.date_range("2021-01-01",periods=n)
    return out


def markets(n=855):
    btc=frame(n)
    eth=btc.copy()
    eth["open"]*=1.15
    eth["high"]*=1.15
    eth["low"]*=1.15
    eth["close"]*=1.15
    return {"KRW-BTC":btc,"KRW-ETH":eth}


def mocked(train,target,fit_end,columns,state_dir):
    out=np.full(len(target),.51)
    out[fit_end:]=.63 if len(columns)>5 else .46
    return out,{
        "validation_max_label_open_index":fit_end-1,
        "outer_fit_cutoff_index":fit_end,
        "train_max_label_open_index":fit_end-121,
        "feature_names":list(columns),
        "trained_on_target_asset":train is target,
    }


def test_fixed_feature_sets_cannot_mutate_operational_nine_columns():
    assert FAMILIES["full_9"]==FEATURE_COLUMNS
    assert FAMILIES["returns_only_4"]==("r1","r3","r5","r10")
    assert "volume_ratio" not in FAMILIES["no_range_volume_7"]
    assert len(set(map(tuple,FAMILIES.values())))==3


def test_identical_dates_and_costs_for_all_families():
    m=run_comparison(markets(),state_dir="unused",trainer=mocked,max_folds=2)
    assert m["status"]=="EXPLORATORY_NO_INDEPENDENT_ALPHA_PROOF"
    assert m["safeguards"]["no_test_tuning"] if "no_test_tuning" in m["safeguards"] else m["oos_scheme"]["no_OOS_tuning"]
    for symbol,body in m["assets"].items():
        assert len(body["folds"])==2
        assert len(body["score_rows"])==2*5*3
        for idx in (0,1):
            fold=[r for r in body["score_rows"] if r["fold"]==idx]
            assert set(r["days"] for r in fold)=={120}
            for key,value in body["folds"][idx]["candidates"].items():
                assert value["maturity"]["validation_max_label_open_index"]<body["folds"][idx]["fit_exclusive"]
                assert value["forecast_quality"]["observations"]==120
        for name in (REFERENCE,)+tuple(FAMILIES)+(TRANSFER,):
            s={r["cost_bps"]:r for r in body["summary"] if r["name"]==name}
            assert s[15]["compounded_isolated_fold_return"]>=s[30]["compounded_isolated_fold_return"]
            assert s[30]["compounded_isolated_fold_return"]>=s[60]["compounded_isolated_fold_return"]
        assert body["multiple_testing"]["eligible_for_automatic_promotion"] is False
    assert m["safeguards"]["real_orders_enabled"] is False


def test_misaligned_markets_fail_closed():
    m=markets()
    m["KRW-ETH"].at[300,"date"]=m["KRW-ETH"].at[301,"date"]
    with pytest.raises(ValueError,match="Duplicate or reordered|Missing calendar|fully aligned"):
        run_comparison(m,state_dir="unused",trainer=mocked,max_folds=1)


def test_qlib_actual_transfer_does_not_fit_target_outcomes(tmp_path):
    pytest.importorskip("qlib")
    m=markets()
    frame_b=m["KRW-BTC"]
    frame_e=m["KRW-ETH"]
    original,proof=fit_predict(frame_b,frame_e,600,FAMILIES["full_9"],tmp_path/"qlib")
    # Perturb only ETH future OHLCV after train cutoff. The BTC training
    # data is identical; earlier ETH inference predictions must be unchanged.
    changed=frame_e.copy()
    changed.loc[600:,["open","high","low","close"]]*=1.10
    changed.loc[600:,"volume"]*=1.17
    alternate,other=fit_predict(frame_b,changed,600,FAMILIES["full_9"],tmp_path/"qlib")
    np.testing.assert_allclose(original[560:600],alternate[560:600],rtol=0,atol=1e-12)
    assert proof["trained_on_target_asset"] is False
    assert other["validation_max_label_open_index"]<600


def test_actual_same_asset_full_model_matches_existing_qlib(tmp_path):
    pytest.importorskip("qlib")
    from quantlab.qlib_bridge import qlib_probabilities
    f=frame()
    baseline=qlib_probabilities(f,600,tmp_path/"qlib")
    actual,proof=fit_predict(f,f,600,FAMILIES["full_9"],tmp_path/"qlib")
    np.testing.assert_allclose(actual[560:700],baseline[560:700],rtol=0,atol=1e-12)
    assert proof["trained_on_target_asset"] is True


def test_cross_asset_training_ignores_source_market_future_after_fit(tmp_path):
    pytest.importorskip('qlib')
    m=markets()
    source=m['KRW-BTC']
    target=m['KRW-ETH']
    original,_=fit_predict(source,target,600,FAMILIES['full_9'],tmp_path/'qlib2')
    altered=source.copy()
    altered.loc[600:,['open','high','low','close']]*=1.35
    altered.loc[600:,'volume']*=1.6
    replay,_=fit_predict(altered,target,600,FAMILIES['full_9'],tmp_path/'qlib2')
    np.testing.assert_allclose(original[600:800],replay[600:800],rtol=0,atol=1e-12)

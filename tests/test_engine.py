import numpy as np
import pandas as pd
import pytest

from quantlab.data import demo_daily, normalize, DataError
from quantlab.engine import Candidate, research, signals, simulate, splits, _model
from quantlab.features import build_features, forward_label
from quantlab.settings import Settings


def test_features_causal_future_close_cannot_change_old_features():
    df = demo_daily(320)
    x = build_features(df)
    changed = df.copy()
    changed.loc[310:, "close"] *= 3
    # Only bar 310 and later can differ; no feature can look ahead.
    pd.testing.assert_frame_equal(x.iloc[:310], build_features(changed).iloc[:310])


def test_labels_require_two_future_opens():
    d = demo_daily(300)
    y = forward_label(d)
    assert pd.isna(y.iloc[-1]) and pd.isna(y.iloc[-2])
    expect = d["open"].iloc[112] / d["open"].iloc[111] > 1
    assert bool(y.iloc[110]) == bool(expect)


def test_split_and_no_test_peeking():
    data = demo_daily(900)
    settings = Settings()
    t, v = splits(data, settings)
    assert (t, v) == (540, 720)
    first = research(data, settings, annual_periods=365)
    edited = data.copy()
    # Change all test-day closes without changing train/validation history.
    edited.loc[v:, "close"] *= 1.02
    edited["high"] = np.maximum(edited["high"], edited["close"])
    edited["low"] = np.minimum(edited["low"], edited["close"])
    second = research(edited, settings, annual_periods=365)
    assert first.get("selected") == second.get("selected")
    assert first.get("validation") == second.get("validation")


def test_next_open_execution_and_costs():
    d = demo_daily(300)
    s = np.zeros(len(d))
    s[100] = 1
    config = Settings(max_position_fraction=0.2, fee_bps=100, slippage_bps=0)
    r = simulate(d, s, 100, 101, config)
    assert r["orders"] == 2  # entry + forced exit
    ret = d["open"].iloc[102] / d["open"].iloc[101] - 1
    expect = (1 - 0.2 * 0.01) * (1 + 0.2 * ret) * (1 - 0.2 * 0.01) - 1
    # Final forced liquidation fee only on allocated fraction (0.2)
    assert r["total_return"] == pytest.approx(expect)
    assert r["total_return"] != pytest.approx(ret)


def test_no_leverage_and_insufficient_bars():
    d = demo_daily(300)
    s = np.ones(len(d)) * 2
    with pytest.raises(ValueError):
        simulate(d, s, 100, 120, Settings())
    with pytest.raises(ValueError):
        research(d.iloc[:200], Settings())


def test_research_outputs_are_testable():
    result = research(demo_daily(850), Settings(), annual_periods=365)
    assert result["status"] in ("PAPER_ELIGIBLE", "REJECT")
    if result["status"] != "REJECT" or result.get("selected"):
        assert result["test_rows"] >= 40
        assert result["strategy_id"]


def test_ohlcv_invalid_is_rejected():
    df = demo_daily(320)
    df.loc[4, "high"] = 0
    with pytest.raises(DataError):
        normalize(df)

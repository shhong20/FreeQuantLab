"""OOS diagnostic invariants: no test-based strategy selection or promotion."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from quantlab.agent_lab import FactorProposal, MockAgent, compare_agents
from quantlab.data import demo_daily
from quantlab.diagnostics import (
    daily_net_returns, trailing_regimes, regime_attribution,
    screen_redundant_factors, paired_block_bootstrap, diagnostic_summary,
)
from quantlab.engine import simulate
from quantlab.features import build_features
from quantlab.settings import Settings


def test_daily_path_equivalent_to_existing_simulate_both_cost_levels():
    df = demo_daily(550)
    sig = np.zeros(len(df), dtype=float)
    sig[300:330] = 1
    sig[350:400:2] = 1
    sig[470:490] = 1
    for cost in (5, 20, 80):
        cfg = Settings(fee_bps=cost, slippage_bps=cost)
        for scale in (0, .5, 1):
            path = daily_net_returns(df, sig, 300, 495, cfg, scale=scale)
            if scale == 0:
                expected = simulate(df, np.zeros(len(df)), 300, 495, cfg)
            else:
                expected = simulate(df, sig, 300, 495,
                                    replace(cfg, max_position_fraction=cfg.max_position_fraction*scale))
            assert np.prod(1+path) - 1 == pytest.approx(expected["total_return"], abs=1e-10)


def test_regime_classification_cannot_see_future_candles():
    df = demo_daily(650)
    before = trailing_regimes(df)
    changed = df.copy()
    changed.loc[550:, ["open","high","low","close"]] *= 3
    after = trailing_regimes(changed)
    pd.testing.assert_frame_equal(before.iloc[:550], after.iloc[:550])


def test_regime_attribution_log_sums_exact_total():
    df = demo_daily(650)
    sig = (df["close"] > df["close"].rolling(25).mean()).to_numpy(dtype=float)
    daily = daily_net_returns(df, sig, 400, 630, Settings())
    attr = regime_attribution(df, daily, 400, 630)
    for axis in ("direction", "volatility"):
        assert sum(x["days"] for x in attr[axis].values()) == len(daily)
        assert sum(x["log_equity_contribution"] for x in attr[axis].values()) == pytest.approx(np.log1p(daily).sum())


def test_redundant_factors_filtered_using_train_only():
    df = demo_daily(650)
    feats = build_features(df)
    props = [
        FactorProposal("p1", "r5", "gt", 0),
        FactorProposal("p2", "r5", "gt", .0000001),
        FactorProposal("p3", "ma30", "gt", 0),
    ]
    selected, info = screen_redundant_factors(props, feats, 400)
    assert len(selected) == 2
    assert selected[0].name == "p1" and selected[1].name == "p3"
    assert info[1]["reason"] == "near_duplicate_training_signal"
    # All market information >= train_end is irrelevant to screening.
    future = df.copy()
    future.loc[420:, ["open","high","low","close"]] *= 9
    second, detail = screen_redundant_factors(props, build_features(future), 400)
    assert [p.name for p in selected] == [p.name for p in second]
    assert info == detail


def test_nearly_constant_and_nonbinary_rejected():
    feats = pd.DataFrame({"a": np.arange(200)})
    class Constant:
        name = "constant"
        def compute(self, frame): return np.ones(len(frame))
    kept, meta = screen_redundant_factors([Constant()], feats, 120)
    assert kept == []
    assert meta[0]["reason"] == "nearly_constant_training_signal"
    class Invalid:
        name = "invalid"
        def compute(self, frame): return np.ones(len(frame)) * 2
    with pytest.raises(ValueError):
        screen_redundant_factors([Invalid()], feats, 120)


def test_bootstrap_paired_fixed_seed_and_negative_observation():
    bull = [np.full(90, -.002), np.full(90, -.001)]
    hold = [np.full(90, .001), np.full(90, .001)]
    a = paired_block_bootstrap(bull, hold, repetitions=250, block_size=10)
    b = paired_block_bootstrap(bull, hold, repetitions=250, block_size=10)
    assert a == b
    assert a["observed_log_excess"] < 0
    assert a["bootstrap_95pct_interval_log_excess"][1] < 0
    assert a["multiple_testing_corrected"] is False
    assert a["eligible_for_automatic_promotion"] is False


def test_invalid_bootstrap_arguments_fail_closed():
    with pytest.raises(ValueError):
        paired_block_bootstrap([np.ones(20)], [np.ones(12)])
    with pytest.raises(ValueError):
        paired_block_bootstrap([np.zeros(30)], [np.zeros(30)], repetitions=20)
    with pytest.raises(ValueError):
        diagnostic_summary([{"alpha": np.ones(10)}])


def test_mock_agent_report_has_regime_and_riskmatched_no_live():
    report = compare_agents(demo_daily(520), Settings(), MockAgent(), annual_periods=365)
    assert len(report["folds"]) == 4
    assert report["real_orders_enabled"] is False
    assert report["promotion_eligible"] is False
    assert report["diagnostics"]["promotes_to_paper_or_live"] is False
    assert report["diagnostics"]["C_vs_riskmatched_buy_hold"]["exploratory_only"] is True
    assert len(report["folds"][0]["factor_screening"]) == 3
    for fold in report["folds"]:
        assert "regime_attribution" in fold
        assert "risk_matched_C_buy_hold" in fold
        assert "buy_hold_riskmatched_C" in fold["regime_attribution"]


def test_mock_diagnostics_future_change_cannot_change_earlier_fold():
    df = demo_daily(520)
    first = compare_agents(df, Settings(), MockAgent(), annual_periods=365)
    changed = df.copy()
    changed.loc[435:, ["open","high","low","close"]] *= 1.7
    second = compare_agents(changed, Settings(), MockAgent(), annual_periods=365)
    assert first["folds"][0] == second["folds"][0]

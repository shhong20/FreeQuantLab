"""Qlib adapter contract and leak-safe split tests; offline CI needs no pyqlib."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.features import build_features
from quantlab.qlib_bridge import (
    _MemoryQlibDataset, qlib_probabilities, qlib_signals, qlib_walk_forward,
)
from quantlab.settings import Settings


def test_in_memory_qlib_dataset_segment_contract():
    feat = pd.DataFrame({"r5": [0.1, 0.2, 0.3]})
    label = pd.DataFrame({"LABEL0": [1, 0, 1]})
    d = _MemoryQlibDataset(
        pd.concat({"feature": feat, "label": label}, axis=1),
        pd.concat({"feature": feat, "label": label}, axis=1),
        feat,
    )
    assert set(d.segments) == {"train", "valid", "test"}
    assert d.prepare("train", ["feature", "label"]).shape == (3, 2)
    assert list(d.prepare("test", "feature")) == ["r5"]
    with pytest.raises(ValueError):
        d.prepare("UNKNOWN")
    with pytest.raises(ValueError):
        d.prepare("test", "unsupported")


def test_qlib_boundary_checks_precede_any_training(tmp_path):
    with pytest.raises(ValueError, match="Invalid Qlib training boundary"):
        qlib_probabilities(demo_daily(300), 249, tmp_path)
    with pytest.raises(ValueError, match="threshold"):
        qlib_signals(demo_daily(300), 260, tmp_path, threshold=1)


def test_qlib_walk_forward_same_four_folds_and_no_live_promotion(monkeypatch, tmp_path):
    def fake_qlib(df, fit_end, state_dir, threshold=0.55):
        # Stand-in to test orchestration only, NOT Qlib model performance.
        c = df["close"]
        return (c > c.rolling(30).mean()).fillna(False).to_numpy(dtype=float)

    monkeypatch.setattr("quantlab.qlib_bridge.qlib_signals", fake_qlib)
    report = qlib_walk_forward(
        demo_daily(520), Settings(), tmp_path, annual_periods=365
    )
    assert report["real_orders_enabled"] is False
    assert report["promotion_eligible"] is False
    assert report["engine"] == "qlib.contrib.model.gbdt.LGBModel"
    assert report["diagnostics"]["promotes_to_paper_or_live"] is False
    assert report["diagnostics"]["comparisons"]["Q_qlib_lgbmodel"]["exploratory_only"] is True
    assert "regime_attribution" in report["folds"][0]["results"]["Q_qlib_lgbmodel"]
    assert len(report["folds"]) == 4
    assert all(
        "Q_qlib_lgbmodel" in fold["results"] for fold in report["folds"]
    )
    assert set(report["out_of_sample_summary"]) == {
        "A_baseline", "Q_qlib_lgbmodel", "buy_hold_same_exposure"
    }
    for value in report["out_of_sample_summary"].values():
        if value["orders"]:
            assert value["compounded_return_2x_cost"] <= value["compounded_return"] + 1e-11


def test_qlib_fixed_threshold_is_not_test_tuned(monkeypatch, tmp_path):
    observed = []
    def fake_qlib(df, fit_end, state_dir, threshold=0.55):
        observed.append((fit_end, threshold))
        return np.ones(len(df), dtype=float)
    monkeypatch.setattr("quantlab.qlib_bridge.qlib_signals", fake_qlib)
    qlib_walk_forward(demo_daily(520), Settings(), tmp_path, annual_periods=365)
    assert len(observed) == 8
    assert all(threshold == 0.55 for _, threshold in observed)
    for i in range(0, 8, 2):
        assert observed[i][0] < observed[i+1][0]

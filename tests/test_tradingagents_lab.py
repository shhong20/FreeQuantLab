"""Offline TradingAgents research adapters: strict provenance, anti-leak, no live orders."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.settings import Settings
from quantlab.tradingagents_lab import (
    TradingAgentsLabError, RESEARCH_ONLY, evaluate, load_candles, load_decisions,
    signal_from_ratings, main,
)


def fixture(tmp_path):
    df = demo_daily(200).copy()
    df["date"] = pd.date_range("2025-01-01", periods=200)
    csv = tmp_path / "daily.csv"
    df.to_csv(csv, index=False)
    rows = []
    for idx in range(155, 197, 4):
        rows.append({
            "ticker": "005930", "decision_date": df.loc[idx, "date"].date().isoformat(),
            "rating": "Buy" if (idx // 4) % 2 else "Sell",
            "generated_at": "2026-10-27T09:00:00+09:00",
            "model_id": "exaone3.5:latest",
            "config_sha256": "a" * 64, "provenance_kind": "retrospective",
        })
    single = tmp_path / "single.jsonl"
    multi = tmp_path / "multi.jsonl"
    for file in (single, multi):
        file.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return df, csv, single, multi, rows


def test_load_and_compare_never_eligible(tmp_path):
    df, csv, single, multi, rows = fixture(tmp_path)
    loaded, fingerprint = load_candles(csv)
    assert len(loaded) == 200 and len(fingerprint) == 64
    valid = set(loaded.date.dt.strftime("%Y-%m-%d"))
    b, sha_b, meta_b = load_decisions(single, "005930", valid)
    c, sha_c, meta_c = load_decisions(multi, "005930", valid)
    assert sha_b == sha_c and meta_b["count"] == len(rows)
    report = evaluate(loaded, ticker="005930", single=b, multi=c)
    assert report["status"] == RESEARCH_ONLY
    assert not any(report[x] for x in ("lookahead_verified", "forward_eligible", "paper_eligible", "real_orders_enabled"))
    assert set(report["comparison"]) == {"A_fixed_MA10_40", "B_single_agent", "C_multi_agent", "D_MA_with_multi_veto"}
    for lane in report["comparison"].values():
        assert lane["stress_2x_cost"]["total_return"] <= lane["total_return"] + 1e-12


def test_decision_contract_rejects_untrusted_and_duplicates(tmp_path):
    df, csv, single, _, rows = fixture(tmp_path)
    frame, _ = load_candles(csv)
    valid = set(frame.date.dt.strftime("%Y-%m-%d"))
    for change in [
        {"rating": "REVIEW"},
        {"provenance_kind": "forward"},
        {"decision_date": "2030-01-01"},
        {"ticker": "OTHER"},
        {"generated_at": "2020-01-01T00:00:00+00:00"},
        {"generated_at": "2026-01-01T00:00:00"},
        {"config_sha256": "hello"},
        {"extra": "do something"},
    ]:
        changed = dict(rows[0], **change)
        single.write_text(json.dumps(changed) + "\n", encoding="utf-8")
        with pytest.raises(TradingAgentsLabError):
            load_decisions(single, "005930", valid)
    single.write_text(json.dumps(rows[0]) + "\n" + json.dumps(rows[0]), encoding="utf-8")
    with pytest.raises(TradingAgentsLabError, match="Duplicate"):
        load_decisions(single, "005930", valid)


def test_candles_reject_duplicates_and_future(tmp_path):
    df, csv, *_ = fixture(tmp_path)
    duplicated = pd.concat((df, df.iloc[[0]]), ignore_index=True)
    duplicated.to_csv(csv, index=False)
    with pytest.raises(TradingAgentsLabError, match="Duplicate"):
        load_candles(csv)
    df.loc[199, "date"] = pd.Timestamp(datetime.now(timezone.utc).date())
    df.to_csv(csv, index=False)
    with pytest.raises(TradingAgentsLabError, match="Current/future"):
        load_candles(csv)


def test_rating_semantics_and_expiry():
    days = pd.date_range("2025-01-01", periods=8)
    src = {"2025-01-01": "Buy", "2025-01-02": "Hold",
           "2025-01-05": "Underweight", "2025-01-07": "Overweight"}
    result = signal_from_ratings(pd.Series(days), src, max_age_sessions=3)
    np.testing.assert_array_equal(result, [1, 1, 1, 1, 0, 0, 1, 1])


def test_evaluation_immune_to_later_history(tmp_path):
    df, csv, single, multi, rows = fixture(tmp_path)
    frame, _ = load_candles(csv)
    valid = set(frame.date.dt.strftime("%Y-%m-%d"))
    b, *_ = load_decisions(single, "005930", valid)
    c, *_ = load_decisions(multi, "005930", valid)
    a = evaluate(frame, ticker="005930", single=b, multi=c)
    # only a later history record after evaluation's last decision is modified
    frame.loc[199, "close"] *= 1.5
    later = evaluate(frame, ticker="005930", single=b, multi=c)
    assert a["comparison"] == later["comparison"]


def test_no_network_in_evaluation(monkeypatch, tmp_path):
    _, csv, single, multi, _ = fixture(tmp_path)
    def forbidden(*_a, **_kw):
        raise AssertionError("Unexpected HTTP request")
    monkeypatch.setattr("requests.get", forbidden)
    monkeypatch.setattr("requests.post", forbidden)
    frame, _ = load_candles(csv)
    valid = set(frame.date.dt.strftime("%Y-%m-%d"))
    b, *_ = load_decisions(single, "005930", valid)
    c, *_ = load_decisions(multi, "005930", valid)
    assert evaluate(frame, ticker="005930", single=b, multi=c)["comparison"]


def test_cli_rejects_any_output_outside_isolated_reports(tmp_path):
    _, csv, single, multi, _ = fixture(tmp_path)
    with pytest.raises(SystemExit) as error:
        main(["--csv", str(csv), "--ticker", "005930", "--single", str(single),
              "--multi", str(multi), "--out", str(tmp_path / "unauthorized.json")])
    assert error.value.code == 2

"""Isolated Alpaca historical provider contract tests. All pages synthetic/mocked."""
from __future__ import annotations

from datetime import datetime, timezone, time
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

from quantlab.intraday_alpaca import (
    SourceError, capture, extract_complete_sessions, fetch_alpaca,
)


NOW = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)
NY = ZoneInfo("America/New_York")
START = "2025-08-01"
END = "2025-12-31"


def history(days=85):
    rows = []
    for d in pd.bdate_range(end="2025-12-30", periods=days):
        local = datetime.combine(d.date(), time(9, 30), NY)
        for i in range(78):
            at = local + pd.Timedelta(minutes=5*i)
            close = 100.0
            rows.append({
                "t": at.astimezone(timezone.utc).isoformat(),
                "o": close, "h": 100.1, "l": 99.9, "c": close,
                "v": 10000,
            })
    return rows


class Response:
    def __init__(self, content, status=200):
        self.content = content
        self.status = status

    def raise_for_status(self):
        if self.status != 200:
            raise requests.HTTPError("unauthorized")


def fake_getter(pages, calls):
    def get(url, **kw):
        calls.append((url, kw))
        page = len(calls) - 1
        assert page < len(pages), "unexpected page after final token"
        return Response(json.dumps(pages[page]).encode())
    return get


def test_alpaca_sip_paginated_capture_atomic_and_readonly(tmp_path):
    data = history()
    first = {"symbol": "SPY", "bars": data[:4200], "next_page_token": "page2"}
    last = {"symbol": "SPY", "bars": data[4200:], "next_page_token": None}
    calls = []
    out = capture(
        symbol="SPY", start=START, end=END, out_dir=tmp_path,
        key="TEST-KEY-SECRET", secret="TEST-SECRET-SECRET",
        feed="sip", now=NOW, getter=fake_getter([first, last], calls),
    )
    d = Path(out["path"])
    assert len(calls) == 2
    assert calls[0][1]["params"]["timeframe"] == "5Min"
    assert calls[0][1]["params"]["feed"] == "sip"
    assert calls[0][1]["params"]["adjustment"] == "raw"
    assert calls[1][1]["params"]["page_token"] == "page2"
    assert calls[0][1]["headers"]["APCA-API-KEY-ID"] == "TEST-KEY-SECRET"
    manifest = json.loads((d/"manifest.json").read_text())
    assert manifest["complete_sessions"] == 85
    assert manifest["pages"] == 2
    assert manifest["excluded_pre_or_post_market_bars"] == 0
    assert manifest["orders_enabled"] is False
    assert manifest["dataset_sha256"] == hashlib.sha256((d/"bars_5m.csv").read_bytes()).hexdigest()
    for i, digest in enumerate(manifest["raw_page_sha256"]):
        assert hashlib.sha256((d/"raw"/f"alpaca_page_{i:04}.json").read_bytes()).hexdigest() == digest
    assert "TEST-SECRET" not in (d/"manifest.json").read_text()
    assert "TEST-SECRET" not in (d/"report.json").read_text()
    report = json.loads((d/"report.json").read_text())
    assert report["status"] == "ISOLATED_INTRADAY_RESEARCH_ONLY_NOT_PAPER_ELIGIBLE"
    assert report["source_sessions"] == 85
    assert not (tmp_path/"paper.db").exists()
    with pytest.raises(SourceError, match="already exists"):
        capture(
            symbol="SPY", start=START, end=END, out_dir=tmp_path,
            key="TEST-KEY-SECRET", secret="TEST-SECRET-SECRET",
            feed="sip", now=NOW, getter=fake_getter([first, last], []),
        )


def test_iex_is_inspection_only_and_never_scores_strategy(tmp_path):
    data = history()
    calls = []
    out = capture(
        symbol="SPY", start=START, end=END, out_dir=tmp_path,
        key="X", secret="Y", feed="iex", now=NOW,
        getter=fake_getter([{"bars": data, "next_page_token": None}], calls),
    )
    path = Path(out["path"])
    report = json.loads((path/"report.json").read_text())
    assert report["status"] == "BLOCKED_IEX_PARTIAL_MARKET_NOT_COMPARABLE"
    assert (path/"manifest.json").exists()
    assert out["manifest"]["feed_coverage_gate"].startswith("IEX_PARTIAL")


def test_missing_or_short_session_excluded_whole_never_imputed():
    data = history()
    incomplete_date = data[78*13]["t"][:10]
    removed_bar = data.pop(78*13+2)
    assert removed_bar["t"][:10] == incomplete_date
    rows = [{
        "timestamp": row["t"], "open": row["o"], "high": row["h"],
        "low": row["l"], "close": row["c"], "volume": row["v"],
    } for row in data]
    clean, audit = extract_complete_sessions(pd.DataFrame(rows), START, END)
    assert audit["complete_sessions"] == 84
    assert len(clean) == 84*78
    assert len(audit["excluded_sessions"]) == 1
    assert "missing_or_noncontiguous" in list(audit["excluded_sessions"].values())[0]


def test_dst_utc_time_is_converted_to_correct_new_york_wallclock():
    data = history(85)
    assert data[0]["t"].endswith("+00:00")
    first_local = datetime.fromisoformat(data[0]["t"]).astimezone(NY)
    assert first_local.hour == 9 and first_local.minute == 30


def test_provider_errors_and_missing_pages_fail_closed(tmp_path):
    data = history()
    cases = [
        [{"bars": data[:78], "next_page_token": "X"},
         {"bars": data[:78], "next_page_token": None}],
        [{"bars": data[:78], "next_page_token": "X"},
         {"bars": [], "next_page_token": "X"}],
        [{"bars": data, "symbol": "BAD", "next_page_token": None}],
        [{"bars": [dict(data[0], v=-5)], "next_page_token": None}],
        [{"bar": data, "next_page_token": None}],
    ]
    for pages in cases:
        with pytest.raises(SourceError):
            capture(
                symbol="SPY", start=START, end=END, out_dir=tmp_path,
                key="X", secret="Y", now=NOW,
                getter=fake_getter(pages, []),
            )
    assert list(tmp_path.iterdir()) == []


def test_no_credential_future_date_or_oversized_range():
    data = history()
    dummy = fake_getter([{"bars": data}], [])
    with pytest.raises(SourceError, match="APCA"):
        fetch_alpaca(symbol="SPY", start=START, end=END,
                     key="", secret="", now=NOW, getter=dummy)
    with pytest.raises(SourceError, match="fully past"):
        fetch_alpaca(symbol="SPY", start="2026-08-01", end="2026-10-26",
                     key="X", secret="Y", now=NOW, getter=dummy)
    with pytest.raises(SourceError, match="range"):
        fetch_alpaca(symbol="SPY", start="2014-01-01", end=END,
                     key="X", secret="Y", now=NOW, getter=dummy)
    with pytest.raises(SourceError, match="ticker"):
        fetch_alpaca(symbol="../SPY", start=START, end=END,
                     key="X", secret="Y", now=NOW, getter=dummy)

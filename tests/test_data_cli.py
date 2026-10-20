from datetime import datetime, timezone

import pandas as pd
import pytest

from quantlab import data
from quantlab.cli import main


class FakeResponse:
    def __init__(self, obj):
        self.obj = obj
    def json(self):
        return self.obj


def test_upbit_excludes_partial_and_paginates(monkeypatch):
    calls = []
    def fake_get(url, **kwargs):
        calls.append(kwargs["params"])
        if len(calls) == 1:
            return FakeResponse([{"candle_date_time_utc": f"2026-10-{j:02d}T00:00:00", "opening_price": 100.0,
                                  "high_price": 110.0, "low_price": 90.0, "trade_price": 103.0,
                                  "candle_acc_trade_volume": 1.0} for j in range(12, 1, -1)])
        return FakeResponse([])
    monkeypatch.setattr(data, "_get", fake_get)
    frame = data.upbit_daily("KRW-BTC", bars=100, now=datetime(2026, 10, 12, 14, tzinfo=timezone.utc))
    assert frame["date"].max() == pd.Timestamp("2026-10-11")
    assert calls[0]["to"] == "2026-10-12T00:00:00Z"


def test_upbit_quote_freshness(monkeypatch):
    stamp = datetime(2026, 10, 12, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(data, "_get", lambda url, **kw: FakeResponse([{
        "market": "KRW-BTC", "trade_price": 12345,
        "trade_timestamp": int(stamp.timestamp() * 1000)}]))
    assert data.live_paper_quote("upbit", "KRW-BTC", now=stamp) == (12345.0, "2026-10-12")
    later = datetime(2026, 10, 12, 2, tzinfo=timezone.utc)
    with pytest.raises(data.DataError, match="stale"):
        data.live_paper_quote("upbit", "KRW-BTC", now=later)


def test_demo_cli_research_runs(tmp_path):
    code = main(["--config", "examples/demo_config.json", "--report-dir", str(tmp_path), "research"])
    assert code == 0
    assert (tmp_path / "demo__SYNTHETIC-ONLY.json").exists()


def test_demo_refuses_paper_freeze(tmp_path):
    code = main(["--config", "examples/demo_config.json", "--state-dir", str(tmp_path),
                 "freeze", "--symbol", "SYNTHETIC-ONLY"])
    assert code == 2


def test_freeze_and_forward_paper_end_to_end_offline(monkeypatch, tmp_path):
    import json
    import sqlite3
    from pathlib import Path
    from quantlab.data import demo_daily
    from quantlab import cli

    cpath = tmp_path / "config.json"
    cpath.write_text(json.dumps({"assets": [{"provider": "upbit", "symbol": "KRW-BTC", "kind": "crypto", "currency": "KRW", "bars": 1000}]}))
    first = demo_daily(1000)
    first["date"] = pd.date_range(end="2026-10-10", periods=len(first))
    second = demo_daily(1001)
    second["date"] = pd.date_range(end="2026-10-11", periods=len(second))
    current = [first]
    monkeypatch.setattr(cli, "fetch", lambda _: current[0])
    monkeypatch.setattr(cli, "live_paper_quote", lambda *args: (100_000.0, "2026-10-12"))
    db = str(tmp_path / "paper.db")
    shared = ["--config", str(cpath), "--state-dir", str(tmp_path / "state"), "--db", db]
    assert cli.main(shared + ["freeze", "--symbol", "KRW-BTC"]) == 0
    assert cli.main(shared + ["paper", "--symbol", "KRW-BTC"]) == 0  # same completed candle: SKIP
    current[0] = second
    assert cli.main(shared + ["paper", "--symbol", "KRW-BTC"]) == 0
    assert cli.main(shared + ["paper", "--symbol", "KRW-BTC"]) == 0  # idempotent
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert cli.main(shared + ["status", "--symbol", "KRW-BTC"]) == 0

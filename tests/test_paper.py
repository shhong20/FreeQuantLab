from datetime import datetime, timezone
import sqlite3

import pytest

from quantlab.paper import connect_db, record_paper, paper_statistics, promotion_gate
from quantlab.settings import Settings


def _record(path, date="2026-10-10", session="2026-10-11", target=1, quote=10000.0, settings=None, sid="frozen123"):
    return record_paper(path, provider="upbit", symbol="KRW-BTC", currency="KRW",
                        decision={"date": date, "target": target, "strategy_id": sid},
                        quote=quote, session_date=session, settings=settings or Settings(),
                        now=datetime(2026, 10, 11, 3, tzinfo=timezone.utc))


def test_paper_idempotent_and_fee(tmp_path):
    path = tmp_path / "ledger.db"
    first = _record(path)
    assert first["status"] == "PAPER_RECORDED"
    assert first["fill"]["action"] == "BUY"
    assert first["fill"]["position_units"] > 0
    assert first["fill"]["cash"] >= 0
    assert _record(path)["status"] == "DUPLICATE_SKIPPED"
    with connect_db(path) as db:
        assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_paper_stale_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="stale"):
        _record(tmp_path / "paper.db", session="2026-10-14")
    with pytest.raises(ValueError, match="Not next"):
        _record(tmp_path / "paper.db", session="2026-10-10")


def test_paper_sells_and_stats(tmp_path):
    path = tmp_path / "paper.db"
    first = _record(path)
    second = _record(path, date="2026-10-11", session="2026-10-12", target=0, quote=10300)
    assert second["fill"]["action"] == "SELL"
    assert second["fill"]["position_units"] == pytest.approx(0)
    stat = paper_statistics(path, "KRW-BTC", "frozen123")
    assert stat["orders"] == 2 and stat["days"] == 2


def test_forward_paper_gate_no_live_execution(tmp_path):
    gate = promotion_gate({"status": "PAPER_ELIGIBLE"},
                          {"days": 31, "orders": 10, "paper_return": 0.02, "paper_max_drawdown": -0.04}, Settings(), manual_authorization=True)
    assert gate["live_candidate"] is True
    assert gate["real_orders_enabled"] is False


def test_broken_currency_or_quote_rejected(tmp_path):
    path = tmp_path / "paper.db"
    _record(path)
    with pytest.raises(ValueError):
        _record(path, date="2026-10-11", session="2026-10-12", quote=0)
    with pytest.raises(ValueError):
        record_paper(path, provider="upbit", symbol="KRW-BTC", currency="USD",
                     decision={"date": "2026-10-11", "target": 0, "strategy_id": "x"},
                     quote=10000, session_date="2026-10-12", settings=Settings())


def test_daily_loss_halts_and_persists(tmp_path):
    path = tmp_path / "paper.db"
    _record(path)
    gate = _record(path, date="2026-10-11", session="2026-10-12", quote=7000)
    assert gate["status"] == "HALTED"
    with connect_db(path) as db:
        assert db.execute("SELECT halted FROM accounts WHERE symbol='KRW-BTC'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1

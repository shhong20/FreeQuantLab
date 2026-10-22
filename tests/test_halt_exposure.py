"""No orders, no database writes, no artificial mark-to-market on frozen HALT accounts."""
from __future__ import annotations
from datetime import datetime, timezone
import sqlite3
import pytest

from test_shadow import ASSETS, T20, T21, T22, frame
from quantlab.halt_exposure import inspect_halted_positions, main
from quantlab.settings import Settings
from quantlab.shadow import bootstrap, run_cycle


def test_halt_account_remains_open_but_pnl_is_not_claimed(tmp_path):
    db=tmp_path/"virtual-only.sqlite3"
    asset=[ASSETS[0]]
    bootstrap(db,asset,Settings(),now=T20,fetch_fn=lambda a:frame("2026-10-19"))
    run_cycle(db,asset,now=T21,fetch_fn=lambda a:frame("2026-10-20"),
              quote_fn=lambda *a,now:(100.0,now.date().isoformat()))
    before=inspect_halted_positions(db,now=T21)
    assert before["status"]=="NO_HALTED_ACCOUNTS"
    run_cycle(db,asset,now=T22,fetch_fn=lambda a:frame("2026-10-21"),
              quote_fn=lambda *a,now:(45.0,now.date().isoformat()))
    with sqlite3.connect(db) as con:
        event_count=con.execute("SELECT count(*) FROM shadow_events").fetchone()[0]
        row=con.execute("SELECT units,halted FROM shadow_accounts "
                        "WHERE label='buy_hold_20pct'").fetchone()
    assert row[0] > 0 and row[1] == 1
    result=inspect_halted_positions(db,now=T22)
    assert result["status"]=="HALTED_OPEN_EXPOSURE_UNMARKED"
    assert result["halted_with_open_inventory"]==1
    assert result["accounts"][0]["units_remaining_at_last_record"] > 0
    assert result["accounts"][0]["last_recorded_equity_is_not_current_equity"]
    assert not result["accounts"][0]["current_liquidation_price_verified"]
    assert not result["real_orders_enabled"]
    assert not result["frozen_90_day_study_modified"]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM shadow_events").fetchone()[0] == event_count
        assert con.execute("SELECT units FROM shadow_accounts WHERE label='buy_hold_20pct'").fetchone()[0] == row[0]


def test_unexpected_halt_without_source_event_is_not_certified(tmp_path):
    db=tmp_path/"virtual-only.sqlite3"
    bootstrap(db,[ASSETS[0]],Settings(),now=T20,
              fetch_fn=lambda a:frame("2026-10-19"))
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shadow_accounts SET halted=1")
    with pytest.raises(ValueError,match="HALT event"):
        inspect_halted_positions(db,now=T22)


def test_halt_report_rejects_naive_time(tmp_path):
    with pytest.raises(ValueError,match="Timezone"):
        inspect_halted_positions(tmp_path/"doesnotexist.db",
                                now=datetime(2026,10,22))


def test_halt_cli_fail_closed_for_missing_db(tmp_path,capsys):
    assert main(["--db",str(tmp_path/"nope.db")])==2
    assert "HALT_EXPOSURE_AUDIT_BLOCKED" in capsys.readouterr().err

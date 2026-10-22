"""Clean cohort checks use only synthetic OHLCV and mocked public tickers."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

import pandas as pd
import pytest

from quantlab.clean_cohort import inspect_clean
from quantlab.data import demo_daily
from quantlab.prospective import lock_protocol, read_protocol
from quantlab.shadow import bootstrap, run_cycle
from quantlab.settings import Settings


ASSETS = [
    {"provider":"upbit","symbol":symbol,"kind":"crypto","currency":"KRW","bars":320}
    for symbol in ("KRW-BTC","KRW-ETH")
]
REGISTRATION = datetime(2026,10,21,2,0,tzinfo=timezone.utc)
FIRST_RUN = datetime(2026,10,23,0,24,tzinfo=timezone.utc)
DUE = datetime(2026,10,23,1,21,tzinfo=timezone.utc)


def bars(end):
    frame = demo_daily(320)
    frame["date"] = pd.date_range(end=end,periods=320)
    return frame


@pytest.fixture()
def cohort(tmp_path):
    db = tmp_path/"clean.db"
    manifest = tmp_path/"new.json"
    new_settings=Settings(paper_min_days=90)
    bootstrap(db,ASSETS,new_settings,now=REGISTRATION,
              fetch_fn=lambda _:bars("2026-10-20"))
    lock_protocol(db,manifest,now=REGISTRATION,days=90)
    return db,manifest


def record(db,manifest,*,now=FIRST_RUN,day="2026-10-22"):
    frame = bars(day)
    return run_cycle(db,ASSETS,now=now,fetch_fn=lambda _:frame.copy(),
                     quote_fn=lambda p,s,*,now:(float(frame["close"].iloc[-1]),now.date().isoformat()),
                     protocol_path=manifest)


def test_clean_cohort_starts_future_only_and_pre_due_empty(cohort):
    db,manifest=cohort
    m=read_protocol(manifest)
    assert m["first_eligible_decision_utc"]=="2026-10-22"
    assert len(m["account_set"])==6
    assert inspect_clean(db,manifest,now=REGISTRATION)["status"]=="WAITING_FIRST_DUE"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]==0
        assert all(cash == 1_000_000 and units == 0
                   for cash,units in con.execute("SELECT cash,units FROM shadow_accounts"))
    assert inspect_clean(db,manifest,now=REGISTRATION)["strategy_returns"] is None


def test_first_due_proves_six_unique_events_and_public_quote(cohort):
    db,manifest=cohort
    assert record(db,manifest)["status"]=="OK"
    result=inspect_clean(db,manifest,now=DUE)
    assert result["status"]=="PASS",result
    assert result["expected_due_account_days"]==6
    assert result["recorded_due_account_days"]==6
    assert result["invalid_due_account_days"]==0
    assert result["strategy_returns"] is None
    assert not result["real_orders_enabled"]
    assert record(db,manifest)["status"]=="OK"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]==6
        assert con.execute("SELECT COUNT(*) FROM shadow_quote_evidence").fetchone()[0]==2


def test_old_day_missed_even_if_newest_day_good(cohort):
    db,manifest=cohort
    # The machine missed the first observation entirely, then restarted.
    result=record(db,manifest,now=datetime(2026,10,24,0,24,tzinfo=timezone.utc),
                  day="2026-10-23")
    assert result["status"]=="OK"
    check=inspect_clean(db,manifest,now=datetime(2026,10,24,1,25,tzinfo=timezone.utc))
    assert check["status"]=="INCOMPLETE"
    assert "CUMULATIVE_DUE_ACCOUNT_DAYS_MISSING" in check["issues"]
    assert check["first_incomplete_decision_utc"]=="2026-10-22"
    assert check["recorded_due_account_days"]==6
    assert check["expected_due_account_days"]==12


def test_prewindow_trade_cannot_be_laundered_into_clean_observations(cohort):
    db,manifest=cohort
    data=bars("2026-10-21")
    before=run_cycle(db,ASSETS,
                     now=datetime(2026,10,22,0,18,tzinfo=timezone.utc),
                     fetch_fn=lambda _:data.copy(),
                     quote_fn=lambda p,s,*,now:(float(data["close"].iloc[-1]),now.date().isoformat()),
                     protocol_path=manifest)
    assert before["results"][0]["status"]=="OUTSIDE_PROTOCOL_WINDOW"
    assert inspect_clean(db,manifest,now=DUE)["status"]=="INCOMPLETE"
    # Only synthesize the premature trade in this offline test. The deployed
    # service always supplies the preregistration manifest to run_cycle.
    old=run_cycle(db,ASSETS,
                  now=datetime(2026,10,22,0,18,tzinfo=timezone.utc),
                  fetch_fn=lambda _:data.copy(),
                  quote_fn=lambda p,s,*,now:(float(data["close"].iloc[-1]),now.date().isoformat()))
    assert old["status"]=="OK"
    result=inspect_clean(db,manifest,now=datetime(2026,10,22,0,32,tzinfo=timezone.utc))
    assert result["status"]=="INCOMPLETE"
    assert "OUTSIDE_WINDOW_VIRTUAL_ACTIVITY" in result["issues"]
    assert result["strategy_returns"] is None


def test_saved_quote_corruption_fails_closed(cohort):
    db,manifest=cohort
    record(db,manifest)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shadow_quote_evidence SET public_ticker_price=1 "
                    "WHERE symbol='KRW-ETH'")
    result=inspect_clean(db,manifest,now=DUE)
    assert result["status"]=="INCOMPLETE"
    assert "CUMULATIVE_QUOTE_OR_TIMESTAMP_EVIDENCE_INVALID" in result["issues"]


def test_protocol_identity_is_independent_of_legacy(cohort,tmp_path):
    db,manifest=cohort
    other=tmp_path/"original.db"
    other.write_bytes(b"legacy immutable sentinel")
    before=other.read_bytes()
    record(db,manifest)
    assert other.read_bytes()==before
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]==6
    assert read_protocol(manifest)["account_set"]


def test_late_reboot_skips_public_ticker_and_never_creates_paper_fills(cohort):
    db,manifest=cohort
    late=datetime(2026,10,23,5,0,tzinfo=timezone.utc)
    r=run_cycle(db,ASSETS,now=late,
                fetch_fn=lambda _:pytest.fail("Late replay must not fetch bars"),
                quote_fn=lambda p,s,*,now:pytest.fail("Late replay must not fetch ticker"),
                protocol_path=manifest)
    assert r["status"]=="OK"
    assert r["results"][0]["status"]=="OUTSIDE_OBSERVATION_WINDOW"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]==0
    after=datetime(2026,10,24,1,21,tzinfo=timezone.utc)
    assert inspect_clean(db,manifest,now=after)["status"]=="INCOMPLETE"


def test_cohort_identity_is_distinct_without_changing_trading_parameters(tmp_path,cohort):
    db,manifest=cohort
    old=tmp_path/"legacy.db"
    bootstrap(old,ASSETS,Settings(),now=REGISTRATION,
              fetch_fn=lambda _:bars("2026-10-20"))
    with sqlite3.connect(old) as con:
        legacy={r[0] for r in con.execute("SELECT track_id FROM shadow_accounts")}
        old_settings=[json.loads(r[0]) for r in con.execute("SELECT settings_json FROM shadow_accounts")]
    with sqlite3.connect(db) as con:
        clean={r[0] for r in con.execute("SELECT track_id FROM shadow_accounts")}
        new_settings=[json.loads(r[0]) for r in con.execute("SELECT settings_json FROM shadow_accounts")]
    assert len(clean)==len(legacy)==6 and clean.isdisjoint(legacy)
    for original,new in zip(sorted(old_settings,key=lambda x:json.dumps(x)),
                            sorted(new_settings,key=lambda x:json.dumps(x))):
        assert original["initial_cash"]==new["initial_cash"]==1_000_000
        assert original["max_position_fraction"]==new["max_position_fraction"]==.2
        assert original["fee_bps"]==new["fee_bps"]==10
        assert original["slippage_bps"]==new["slippage_bps"]==5
        assert original["paper_min_days"]==30 and new["paper_min_days"]==90

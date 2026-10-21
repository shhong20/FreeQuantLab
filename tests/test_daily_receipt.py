"""Offline-only daily acceptance tests: no historical fills or brokerage calls."""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
from pathlib import Path
import sqlite3

import pytest

from quantlab import daily_receipt as receipt

T21=datetime(2026,10,21,1,45,tzinfo=timezone.utc)
T22=datetime(2026,10,22,1,45,tzinfo=timezone.utc)


@pytest.fixture()
def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(receipt,"read_protocol",lambda _:{
        "first_eligible_decision_utc":"2026-10-21",
        "last_eligible_decision_utc":"2027-01-18",
    })
    monkeypatch.setattr(receipt,"audit_shadow_ledger",
                        lambda *a,**k:{"status":"PASS"})
    monkeypatch.setattr(receipt,"execution_proxy_quality",
                        lambda *a,**k:{"status":"PASS"})
    monkeypatch.setattr(receipt,"verify_plan",lambda *a,**k:{"entries":[]})
    monkeypatch.setattr(receipt,"audit_decisions",lambda *a,**k:{
        "status":"PASS","fully_replayed_decisions":2})
    monkeypatch.setattr(receipt,"audit_archive",lambda *a,**k:{"status":"PASS"})
    monkeypatch.setattr(receipt,"assess_collection",lambda *a,**k:{
        "status":"BREACH_OR_MISSING",
        "issues":[{"code":"PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY",
                   "prewindow_events":6,"prewindow_virtual_orders":4,
                   "first_unexpected_decision_utc":"2026-10-20"}],
    })
    paths={}
    for lane,basename,n in receipt.LANES:
        p=tmp_path/basename.split("/")[-1]
        paths[lane]=p
        with sqlite3.connect(p) as c:
            c.executescript("""
                CREATE TABLE shadow_accounts(track_id TEXT,symbol TEXT,label TEXT);
                CREATE TABLE shadow_events(track_id TEXT,decision_date TEXT,
                  session_date TEXT,created_at TEXT,action TEXT,target INTEGER,
                  observed_price REAL);
                CREATE TABLE shadow_quote_evidence(symbol TEXT,decision_date TEXT,
                  session_date TEXT,public_ticker_price REAL);
            """)
            for asset in receipt.ASSETS:
                for i in range(n):
                    c.execute("INSERT INTO shadow_accounts VALUES(?,?,?)",
                              (f"{lane}-{asset}-{i}",asset,f"strategy-{i}"))
    return paths,lambda now:receipt.inspect_receipt(
        now=now,original_db=paths["original_90d"],
        agent_db=paths["exaone"],qlib_db=paths["qlib"],
        protocol_file=tmp_path/"protocol.json",qlib_plan=tmp_path/"qlib.json",
        archive_db=tmp_path/"market_history.db")


def put_day(db,day,created,*,strategy_count,quote=100.):
    session=(datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat()
    with sqlite3.connect(db) as c:
        for symbol in receipt.ASSETS:
            c.execute("INSERT INTO shadow_quote_evidence VALUES(?,?,?,?)",
                      (symbol,day,session,quote))
        for track,symbol in c.execute("SELECT track_id,symbol FROM shadow_accounts"):
            c.execute("INSERT INTO shadow_events VALUES(?,?,?,?,?,?,?)",
                      (track,day,session,created,"HOLD",0,quote))


def test_21st_first_isolated_day_passes_and_formal_baseline_not_due(setup):
    paths,check=setup
    first="2026-10-20"
    put_day(paths["exaone"],first,"2026-10-21T00:18:00+00:00",strategy_count=1)
    put_day(paths["qlib"],first,"2026-10-21T00:21:00+00:00",strategy_count=1)
    r=check(T21)
    assert r["status"]=="PASS",r["issues"]
    assert r["due_virtual_account_days"]==4
    assert r["lanes"][0]["status"]=="WAITING_FIRST_DUE"
    assert all(x["status"]=="PASS" for x in r["lanes"][1:])
    assert r["qlib_replayed_model_decisions"]==2
    assert r["research_integrity_status"]=="PREWINDOW_REVIEW_REQUIRED"
    assert r["original_90d_returns"] is None
    assert not r["real_orders_enabled"]


def test_22nd_formal_baseline_requires_six_separate_events(setup):
    paths,check=setup
    put_day(paths["exaone"],"2026-10-20","2026-10-21T00:18:00+00:00",strategy_count=1)
    put_day(paths["qlib"],"2026-10-20","2026-10-21T00:21:00+00:00",strategy_count=1)
    put_day(paths["exaone"],"2026-10-21","2026-10-22T00:18:00+00:00",strategy_count=1)
    put_day(paths["qlib"],"2026-10-21","2026-10-22T00:21:00+00:00",strategy_count=1)
    missing=check(T22)
    assert missing["status"]=="INCOMPLETE"
    assert missing["due_virtual_account_days"]==10
    assert any(s.startswith("original_90d:") for s in missing["issues"])
    put_day(paths["original_90d"],"2026-10-21",
            "2026-10-22T00:15:00+00:00",strategy_count=3)
    done=check(T22)
    assert done["status"]=="PASS",done["issues"]
    assert done["due_virtual_account_days"]==10
    assert len(done["lanes"][0]["due_date_evidence"])==2
    assert all(x["recorded_events"]==3 for x in done["lanes"][0]["due_date_evidence"])
    assert done["research_integrity_status"]=="PREWINDOW_REVIEW_REQUIRED"
    assert done["original_90d_returns"] is None


def test_missing_quote_stale_time_and_nonunique_account_fail_closed(setup):
    paths,check=setup
    put_day(paths["exaone"],"2026-10-20","2026-10-21T00:18:00+00:00",strategy_count=1)
    put_day(paths["qlib"],"2026-10-20","2026-10-21T00:21:00+00:00",strategy_count=1)
    with sqlite3.connect(paths["qlib"]) as c:
        c.execute("UPDATE shadow_events SET created_at='2026-10-21T10:55:00+00:00' "
                  "WHERE track_id LIKE '%KRW-BTC%'")
        c.execute("DELETE FROM shadow_quote_evidence WHERE symbol='KRW-ETH'")
    r=check(T21)
    assert r["status"]=="INCOMPLETE"
    assert any("INVALID_PUBLIC_QUOTE_OR_OBSERVATION_TIME" in s for s in r["issues"])
    with sqlite3.connect(paths["exaone"]) as c:
        line=c.execute("SELECT * FROM shadow_events LIMIT 1").fetchone()
        c.execute("INSERT INTO shadow_events VALUES(?,?,?,?,?,?,?)",line)
    second=check(T21)
    assert second["status"]=="INCOMPLETE"
    assert any("EXPECTED_VIRTUAL_OBSERVATIONS_MISSING" in s for s in second["issues"])


def test_no_due_day_before_grace_period_cannot_claim_observation(setup):
    _,check=setup
    r=check(datetime(2026,10,21,1,19,tzinfo=timezone.utc))
    assert r["status"]=="WAITING_FIRST_DUE"
    assert r["due_virtual_account_days"]==0


def test_report_is_atomic_stores_only_readonly_evidence(tmp_path,setup):
    _,check=setup
    r=check(T21)
    path=tmp_path/"reports"/"2026-10-21.json"
    receipt._safe_report(path,r)
    import json
    recorded=json.loads(path.read_text())
    assert recorded["status"]=="INCOMPLETE"
    assert recorded["original_90d_returns"] is None
    assert path.stat().st_mode&0o077==0


def test_prior_missing_or_tampered_day_detected_even_when_latest_day_is_complete(setup):
    paths, check = setup
    for day in ("2026-10-20", "2026-10-21", "2026-10-22"):
        session = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
        put_day(paths["exaone"], day, session + "T00:18:00+00:00", strategy_count=1)
        put_day(paths["qlib"], day, session + "T00:21:00+00:00", strategy_count=1)
        if day != "2026-10-20":
            put_day(paths["original_90d"], day, session + "T00:15:00+00:00",
                    strategy_count=3)
    t23 = datetime(2026, 10, 23, 1, 45, tzinfo=timezone.utc)
    clean = check(t23)
    assert clean["status"] == "PASS", clean["issues"]
    assert [x["due_history"]["expected_account_days"] for x in clean["lanes"]] == [12, 6, 6]
    assert all(x["due_history"]["missing_account_days"] == 0 for x in clean["lanes"])
    with sqlite3.connect(paths["qlib"]) as con:
        con.execute("DELETE FROM shadow_events WHERE decision_date='2026-10-21' "
                    "AND track_id LIKE '%KRW-BTC%'")
    with sqlite3.connect(paths["exaone"]) as con:
        con.execute("UPDATE shadow_events SET observed_price=123.0 "
                    "WHERE decision_date='2026-10-20' AND track_id LIKE '%KRW-ETH%'")
    result = check(t23)
    assert result["status"] == "INCOMPLETE"
    assert "qlib:HISTORICAL_VIRTUAL_OBSERVATIONS_MISSING" in result["issues"]
    assert "exaone:HISTORICAL_VIRTUAL_EVIDENCE_INVALID" in result["issues"]
    assert result["lanes"][2]["due_history"]["first_missing_utc_decision"] == "2026-10-21"
    assert result["lanes"][1]["due_history"]["first_invalid_utc_decision"] == "2026-10-20"
    assert all(x["due_date_evidence"][0]["recorded_events"] > 0 for x in result["lanes"])
    assert result["original_90d_returns"] is None
    assert result["real_orders_enabled"] is False

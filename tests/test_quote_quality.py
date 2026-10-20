"""Virtual quote evidence quality: no assumptions about executable fills."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pandas as pd

from quantlab.data import demo_daily
from quantlab.quote_quality import execution_proxy_quality
from quantlab.shadow import bootstrap,run_cycle
from quantlab.settings import Settings
from quantlab.watchdog import check

A={"provider":"upbit","symbol":"KRW-BTC","kind":"crypto","currency":"KRW","bars":320}
T20=datetime(2026,10,20,2,tzinfo=timezone.utc)
T21=datetime(2026,10,21,0,15,tzinfo=timezone.utc)


def candles(date):
    d=demo_daily(320)
    d["date"]=pd.date_range(end=date,periods=len(d))
    return d


def register(db):
    bootstrap(db,[A],Settings(),now=T20,fetch_fn=lambda _:candles("2026-10-19"))


def advance(db):
    d=candles("2026-10-20")
    price=float(d["close"].iloc[-1])
    result=run_cycle(db,[A],now=T21,fetch_fn=lambda _:d,
       quote_fn=lambda p,s,*,now:(price,now.date().isoformat()))
    assert result["status"]=="OK"


def test_evidence_absent_before_next_completed_bar(tmp_path):
    db=tmp_path/"shadow.db"
    register(db)
    x=execution_proxy_quality(db)
    assert x["status"]=="AWAITING_FIRST_OBSERVATION"
    assert x["quote_rows"]==0
    assert x["model_assumed_total_cost_krw"]==0
    assert x["not_actual_fill_quality"]
    assert x["no_executable_spread_or_order_book_evidence"]


def test_one_saved_quote_per_symbol_day_for_three_virtual_accounts(tmp_path):
    db=tmp_path/"shadow.db"
    register(db)
    advance(db)
    q=execution_proxy_quality(db)
    assert q["status"]=="PASS",q["issues"]
    assert q["virtual_event_rows"]==3 and q["events_with_quote_evidence"]==3
    assert q["quote_rows"]==1
    assert q["virtual_buy_sell_orders"]>=1
    assert q["model_assumed_total_cost_krw"]>0
    assert q["assumed_cost_rate_bps"]==15
    assert q["max_abs_public_ticker_prev_close_gap_bps"]==0
    assert q["max_public_ticker_minutes_after_close"]==15
    with sqlite3.connect(db) as con:
        old=con.execute("SELECT * FROM shadow_quote_evidence").fetchall()
    again=run_cycle(db,[A],now=T21,fetch_fn=lambda _:candles("2026-10-20"),
                    quote_fn=lambda *args,**kwargs: (_ for _ in ()).throw(
                        AssertionError("Duplicate must not request a quote")))
    assert again["status"]=="OK"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT * FROM shadow_quote_evidence").fetchall()==old


def test_quote_price_tampering_blocks_daily_performance_report(tmp_path):
    db=tmp_path/"shadow.db"
    register(db);advance(db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shadow_quote_evidence SET public_ticker_price=public_ticker_price*1.1")
    q=execution_proxy_quality(db)
    assert q["status"]=="FAIL"
    assert any(x["code"]=="QUOTE_EVENT_PRICE_MISMATCH" for x in q["issues"])
    out=check(shadow_db=db,state_db=tmp_path/"watchdog.db",
              report_dir=tmp_path/"reports",now=T21,windows_notifications=False,
              health_check=lambda:True)
    assert out["status"]=="DEGRADED"
    assert not out["reports"]
    assert any(t["code"]=="PAPER_QUOTE_EVIDENCE_MISMATCH" for t in out["alert_transitions"])


def test_legacy_events_without_quote_rows_are_not_misrepresented(tmp_path):
    db=tmp_path/"shadow.db"
    register(db);advance(db)
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM shadow_quote_evidence")
    q=execution_proxy_quality(db)
    assert q["status"]=="PASS"
    assert q["events_with_quote_evidence"]==0
    assert q["legacy_events_without_quote_evidence"]==3
    assert q["assumed_cost_rate_bps"]==15
    assert q["max_public_ticker_minutes_after_close"] is None


def test_quote_evidence_without_event_is_flagged(tmp_path):
    db=tmp_path/"shadow.db"
    register(db)
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO shadow_quote_evidence VALUES(?,?,?,?,?,?,?)",
           ("KRW-BTC","2026-10-20","2026-10-21",100.,101.,T21.isoformat(),"upbit_public_ticker"))
    q=execution_proxy_quality(db)
    assert any(x["code"]=="QUOTE_EVIDENCE_HAS_NO_EVENT" for x in q["issues"])
    assert q["not_actual_fill_quality"]

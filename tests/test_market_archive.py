"""Immutable real public daily market data recovery; absolutely no paper fill backdating."""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
import sqlite3

import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.market_archive import catch_up,audit_archive,ArchiveError


T21=datetime(2026,10,21,0,10,tzinfo=timezone.utc)
A="KRW-BTC"
B="KRW-ETH"


def frame(day,*,rows=150,drift=0.):
    # A deterministic *global* historical tape: overlapping session dates
    # must retain the same prices when the simulated WSL clock advances.
    full=demo_daily(1000)
    full["date"]=pd.date_range(end="2028-01-01",periods=len(full))
    d=full.loc[full["date"]<=pd.Timestamp(day)].tail(rows).reset_index(drop=True).copy()
    assert len(d)==rows, "synthetic history fixture is too short"
    if drift:
        d.loc[len(d)-3,"close"]+=drift
        d.loc[len(d)-3,"high"]+=drift
    return d


@pytest.fixture()
def setup(tmp_path):
    db=tmp_path/"market_history.db"
    base=frame("2026-10-20")
    calls=[]
    def get(symbol,bars,*,now):
        assert bars==800
        calls.append((symbol,now.date()))
        return base.copy()
    out=catch_up(db,now=T21,fetch_fn=get)
    assert out["status"]=="PASS",out["issues"]
    assert len(calls)==2
    return db,base


def test_historical_initial_and_repeated_fetch_no_new_rows(setup):
    db,base=setup
    result=catch_up(db,now=T21,fetch_fn=lambda *a,**kw:base.copy())
    assert result["status"]=="PASS"
    assert [e["new_historical_candles"] for e in result["assets"]]==[0,0]
    audit=audit_archive(db,now=T21)
    assert audit["status"]=="PASS",audit
    assert audit["rows"]==300
    assert all(x["historical_catchup_rows"]==0 for x in audit["assets"])
    assert audit["real_orders_enabled"] is False
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM archive_runs").fetchone()[0]==2
        assert con.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='shadow_events'"
        ).fetchone()[0]==0


def test_recover_multiple_missed_utc_market_days_after_wsl_shutdown(setup):
    db,_=setup
    later=datetime(2026,10,24,0,25,tzinfo=timezone.utc)
    out=catch_up(db,now=later,fetch_fn=lambda *a,**kw:frame("2026-10-23"))
    assert out["status"]=="PASS",out["issues"]
    assert all(x["new_historical_candles"]==3 for x in out["assets"])
    assert all(x["oldest_recovered_utc_day"]=="2026-10-21" for x in out["assets"])
    assert all(x["newest_recovered_utc_day"]=="2026-10-23" for x in out["assets"])
    check=audit_archive(db,now=later)
    assert check["status"]=="PASS",check
    assert check["rows"]==306
    assert all(x["historical_catchup_rows"]==3 for x in check["assets"])
    assert check["historical_trade_execution_observed"] is False
    # Date/time first retrieved is genuine recovery time, not claimed original execution.
    with sqlite3.connect(db) as con:
        rows=con.execute(
            "SELECT utc_day,provenance,first_retrieved_at_utc "
            "FROM archive_candles WHERE symbol=? ORDER BY utc_day DESC LIMIT 3",
            (A,),
        ).fetchall()
    assert all(x[1]=="historical_catchup" for x in rows)
    assert all(x[2]==later.isoformat() for x in rows)


def test_provider_revises_overlap_cannot_silently_rewrite_immutable_history(setup):
    db,base=setup
    bad=base.copy()
    idx=len(bad)-7
    bad.loc[idx,"close"]=float(bad.loc[idx,"close"])*1.01
    bad.loc[idx,"high"]=max(float(bad.loc[idx,"high"]),float(bad.loc[idx,"close"])*1.002)
    out=catch_up(db,now=T21,fetch_fn=lambda *a,**kw:bad.copy())
    assert out["status"]=="FAIL"
    assert {x["symbol"] for x in out["issues"]}=={A,B}
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM archive_candles").fetchone()[0]==300
        assert con.execute(
            "SELECT count(*) FROM archive_candles WHERE symbol=? AND utc_day=?",
            (A,base.iloc[idx]["date"].date().isoformat()),
        ).fetchone()[0]==1
    assert audit_archive(db,now=T21)["status"]=="REVIEW"


def test_large_offline_gap_beyond_download_limit_fails_closed(setup):
    db,_=setup
    late=T21+timedelta(days=155)
    day=(late.date()-timedelta(days=1)).isoformat()
    out=catch_up(db,now=late,fetch_fn=lambda *a,**kw:frame(day))
    assert out["status"]=="FAIL"
    assert all("manual historical resynchronization" in x["explanation"] for x in out["issues"])
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM archive_candles").fetchone()[0]==300
    assert audit_archive(db,now=late)["status"]=="REVIEW"


def test_bad_candle_or_failed_provider_does_not_mutate_other_asset(setup):
    db,base=setup
    tomorrow=T21+timedelta(days=1)
    d=frame("2026-10-21")
    def source(symbol,bars,*,now):
        if symbol==A:
            raise ConnectionError("Simulated disconnected Upbit")
        return d.copy()
    report=catch_up(db,now=tomorrow,fetch_fn=source)
    assert report["status"]=="FAIL"
    assert report["assets"][0]["symbol"]==B
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT max(utc_day) FROM archive_candles WHERE symbol=?", (A,)
        ).fetchone()[0]=="2026-10-20"
        assert con.execute(
            "SELECT max(utc_day) FROM archive_candles WHERE symbol=?", (B,)
        ).fetchone()[0]=="2026-10-21"
    # A-only retry cannot generate any BTC paper order or edit B's stored candles.
    restored=catch_up(db,now=tomorrow,fetch_fn=lambda *a,**kw:d.copy())
    assert restored["status"]=="PASS"
    assert [x["new_historical_candles"] for x in restored["assets"]]==[1,0]


def test_missing_db_and_wrong_virtual_account_path_are_rejected(tmp_path):
    path=tmp_path/"market_history.db"
    assert audit_archive(path)["status"]=="NOT_INITIALIZED"
    assert not path.exists()
    with pytest.raises(ArchiveError,match="must not point"):
        catch_up(tmp_path/"shadow.db",now=T21,fetch_fn=lambda *a,**kw:None)
    assert not (tmp_path/"shadow.db").exists()


def test_malformed_historical_candles_refused_without_trade_creation(setup):
    db,base=setup
    bad=base.iloc[:-1].copy()
    bad.loc[len(bad)-1,"date"]=pd.Timestamp("2026-10-18")
    result=catch_up(db,now=T21,fetch_fn=lambda *a,**kw:bad.copy())
    assert result["status"]=="FAIL"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM archive_candles").fetchone()[0]==300
    assert audit_archive(db,now=T21)["status"]=="REVIEW"

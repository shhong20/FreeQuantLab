"""Isolated long stock price evidence: no live/paper trading or silent revisions."""
from __future__ import annotations

from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from quantlab import stock_long_history as h
from quantlab.stock_daily_observer import capture
from test_stock_daily_observer import NOW,naver,yahoo,Response

DAYS=[x.date().isoformat() for x in pd.bdate_range(end="2026-10-21",periods=750)]
assert DAYS[-3:]==["2026-10-19","2026-10-20","2026-10-21"]


def synthetic_naver(url,**kwargs):
    symbol=kwargs["params"]["symbol"]
    assert symbol in ("005930","000660","069500")
    rows=[["날짜","시가","고가","저가","종가","거래량","외국인소진율"]]
    for i,day in enumerate(DAYS):
        price=100+i*.01
        rows.append([day.replace("-",""),price,price+1,price-1,price,100+i,10])
    tail=[
        (100,105,99,101,123),
        (102,108,101,105,200),
        (108,111,105,110,300),
    ]
    for i,values in enumerate(tail, len(rows)-3):
        rows[i][1:6]=list(values)
    return Response(("\n"+repr(rows)+"\n").encode(),url)


def synthetic_yahoo(url,**kwargs):
    ticker=url.rsplit("/",1)[-1]
    assert ticker in ("AAPL","MSFT","SPY")
    days=DAYS
    stamps=[int(pd.Timestamp(day+"T13:30:00Z").timestamp()) for day in days]
    prices=[100+i*.01 for i in range(len(days))]
    vals=[100.,101.,105.]
    prices[-3:]=vals
    quote={
        "open":[v-1 for v in prices],
        "high":[v+1 for v in prices],
        "low":[v-2 for v in prices],
        "close":prices,
        "volume":[100+i for i in range(len(days))],
    }
    quote["volume"][-3:]=[100,200,300]
    adjusted=[v*.995 for v in prices]
    result={
      "meta":{"symbol":ticker,"currency":"USD",
              "exchangeTimezoneName":"America/New_York"},
      "timestamp":stamps,
      "indicators":{"quote":[quote],"adjclose":[{"adjclose":adjusted}]},
      "events":{"dividends":{"1":{"date":stamps[-10],"amount":.27}},
                "splits":{}}
    }
    return Response(json.dumps({"chart":{"error":None,"result":[result]}}).encode(),url)


def fixture(tmp_path):
    original=tmp_path/"stock_daily"/"observations.sqlite3"
    r=capture(original,now=NOW,naver_getter=naver,yahoo_getter=yahoo)
    assert r["status"]=="STOCK_PUBLIC_DAILY_DATA_CAPTURED_NO_TRADING"
    history=original.with_name("history.sqlite3")
    return original,history


def run_history(original,history,**overrides):
    return h.capture_history(
        source_db=original,history_db=history,now=NOW,
        naver_getter=overrides.get("naver_getter",synthetic_naver),
        yahoo_getter=overrides.get("yahoo_getter",synthetic_yahoo))


def test_fill_750_days_per_asset_all_short_overlap_immutable(tmp_path):
    original,history=fixture(tmp_path)
    r=run_history(original,history)
    assert r["status"]=="LONG_VENDOR_HISTORY_WITH_ISSUER_AND_TOTAL_RETURN_GATE_BLOCKED"
    assert len(r["assets"])==6
    assert sum(x["new_immutable_bars"] for x in r["assets"])==4497 # US removes Oct21 while incomplete
    assert all(x["overlap_mismatches"]==0 for x in r["assets"])
    assert all(x["overlap_compared"]==3 for x in r["assets"] if x["market"]=="KRX")
    state=h.audit_history(history)
    assert state["status"]=="LONG_HISTORY_LENGTH_VERIFIED_NOT_MODEL_ELIGIBILITY"
    assert state["all_6_assets_have_sufficient_price_history_length"]
    assert state["material_price_or_adjustment_conflicts"]==0
    assert state["sha256_all_history_source_payloads_verified"]
    assert not state["qlib_forward_study_approved"]
    assert not state["approved_official_price_corporate_action_history"]
    with sqlite3.connect("file:"+str(original)+"?mode=ro",uri=True) as c:
        assert c.execute("SELECT count(*) FROM daily_bars").fetchone()[0]==15


def test_second_capture_idempotent_and_retains_vendor_only_actions(tmp_path):
    original,history=fixture(tmp_path)
    run_history(original,history)
    reread=run_history(original,history)
    assert reread["status"]=="LONG_VENDOR_HISTORY_WITH_ISSUER_AND_TOTAL_RETURN_GATE_BLOCKED"
    assert sum(x["new_immutable_bars"] for x in reread["assets"])==0
    audit=h.audit_history(history)
    assert audit["stored_raw_snapshots"]==6
    assert sum(v["vendor_action_events"] for vs in audit["markets"].values()
               for v in vs.values())==3
    assert sum(v["vendor_adjusted_close_points"] for vs in audit["markets"].values()
               for v in vs.values())==3*749


def test_live_observation_price_mismatch_is_recorded_without_rewrite(tmp_path):
    original,history=fixture(tmp_path)
    def changed(url,**kwargs):
        r=synthetic_naver(url,**kwargs)
        data=__import__("ast").literal_eval(r.content.decode().strip())
        data[-3][4]+=1 # 2026-10-19 immutable close differs
        r.content=repr(data).encode()
        return r
    response=run_history(original,history,naver_getter=changed)
    assert response["status"]=="LONG_VENDOR_HISTORY_REVIEW_REQUIRED"
    assert sum(x["overlap_mismatches"] for x in response["assets"])==3
    audit=h.audit_history(history)
    assert audit["material_price_or_adjustment_conflicts"]==3
    with sqlite3.connect("file:"+str(original)+"?mode=ro",uri=True) as conn:
        assert conn.execute("SELECT close FROM daily_bars WHERE ticker='005930' AND session_date='2026-10-19'").fetchone()[0]==101


def test_vendor_adjusted_precision_jitter_retained_but_not_material(tmp_path):
    original,history=fixture(tmp_path)
    run_history(original,history)
    def jitter(url,**kwargs):
        r=synthetic_yahoo(url,**kwargs)
        d=json.loads(r.content)
        d["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"][0]+=0.00002
        r.content=json.dumps(d).encode()
        return r
    r=run_history(original,history,yahoo_getter=jitter)
    assert r["status"]=="LONG_VENDOR_HISTORY_WITH_ISSUER_AND_TOTAL_RETURN_GATE_BLOCKED"
    assert h.audit_history(history)["material_price_or_adjustment_conflicts"]==0
    def big_change(url,**kwargs):
        r=synthetic_yahoo(url,**kwargs)
        d=json.loads(r.content)
        d["chart"]["result"][0]["indicators"]["adjclose"][0]["adjclose"][0]*=1.08
        r.content=json.dumps(d).encode()
        return r
    r=run_history(original,history,yahoo_getter=big_change)
    assert r["status"]=="LONG_VENDOR_HISTORY_REVIEW_REQUIRED"
    assert h.audit_history(history)["material_price_or_adjustment_conflicts"]==3


def test_corrupted_raw_archive_and_bad_sources_cannot_pass(tmp_path):
    original,history=fixture(tmp_path)
    run_history(original,history)
    with sqlite3.connect(history) as con:
        con.execute("UPDATE raw_history SET raw=? WHERE id=1",(b"MUTATED",))
    assert not h.audit_history(history)["sha256_all_history_source_payloads_verified"]
    with pytest.raises(ValueError,match="Independent long stock"):
        h.capture_history(history_db=tmp_path/"shadow.db",source_db=original)
    with pytest.raises(ValueError,match="Reject crypto"):
        h.capture_history(history_db=history,source_db=tmp_path/"shadow.db")


def test_invalid_long_history_and_stock_vendor_meta_refused():
    with pytest.raises(ValueError,match="outside"):
        h._validate_all([("2026-10-20",100,101,99,100,100)],"2026-10-21")
    with pytest.raises(ValueError,match="Unsupported"):
        h.fetch_long("KRX","INVALID",NOW,getter=synthetic_naver)
    def wrong(url,**kwargs):
        data=json.loads(synthetic_yahoo(url,**kwargs).content)
        data["chart"]["result"][0]["meta"]["currency"]="KRW"
        return Response(json.dumps(data).encode(),url)
    with pytest.raises(ValueError,match="identity"):
        h.fetch_long("NASDAQ_NYSE","AAPL",NOW,getter=wrong)

def test_long_vendor_history_updates_daily_quality_gate_but_not_paper_order(tmp_path):
    from quantlab import stock_evidence_quality as evidence
    original,history=fixture(tmp_path)
    run_history(original,history)
    report=evidence.inspect_stock_evidence(original)
    assert report["independent_long_history"]["all_six_662_session_price_lengths_met"]
    assert report["independent_long_history"]["all_raw_payload_SHA256_verified"]
    assert report["independent_long_history"]["material_conflicts"]==0
    assert all(v["long_vendor_history_length_662_met"]
               for values in report["markets"].values() for v in values.values())
    assert all("QLIB_MINIMUM_HISTORICAL_TRAIN_SESSIONS_NOT_MET" not in v["blocks"]
               for values in report["markets"].values() for v in values.values())
    assert "NO_CAUSAL_662_SESSION_STOCK_MODEL_HISTORY" not in report["unresolved_issues"]
    assert not report["total_return_performance_publishable"]
    assert not report["90_day_forward_qlib_registered"]
    assert report["stock_paper_orders_placed"] == 0


def test_long_vendor_history_raw_corruption_blocks_daily_audit_too(tmp_path):
    from quantlab import stock_evidence_quality as evidence
    original,history=fixture(tmp_path)
    run_history(original,history)
    with sqlite3.connect(history) as con:
        con.execute("UPDATE raw_history SET raw=? WHERE id=1",(b"broken raw",))
    with pytest.raises(ValueError,match="Long stock evidence raw SHA256"):
        evidence.inspect_stock_evidence(original)


def test_weekly_stock_history_systemd_units_are_unprivileged_and_nontrading():
    root=Path(__file__).resolve().parents[1]/"deploy"/"systemd"
    service=(root/"freequant-stock-history.service").read_text()
    timer=(root/"freequant-stock-history.timer").read_text()
    daily=(root/"freequant-stock-observer.service").read_text()
    assert "--capture" in service and "--audit" in service
    assert "stock_long_history" in service
    assert "ProtectSystem=strict" in service
    assert "NoNewPrivileges=true" in service
    assert "ReadWritePaths=/home/hong/apps/FreeQuantLab/state/stock_daily" in service
    assert "TimeoutStartSec=210" in service
    assert "OnCalendar=Sat *-*-* 11:40:00" in timer
    assert "Persistent=true" in timer
    assert daily.count("ExecStart=")==4
    assert "stock_long_history --audit" in daily
    assert "shadow.db" not in service
    assert "real-order" not in service

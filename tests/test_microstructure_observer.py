"""Read-only 10-minute L2+tape collector: crash, duplication, corruption, gap contracts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from pathlib import Path

import pytest

from quantlab.continuous_portfolio import ASSETS
from quantlab.microstructure_observer import (
    capture_once,inspect,utcslot,_db,CADENCE_SECONDS,OBSERVATION_GATE_SAMPLES,
)


def clock_at(slot:int)->datetime:
    return datetime.fromtimestamp(slot*CADENCE_SECONDS+20,tz=timezone.utc)


def fake_sources(now:datetime,*,tape_issue=None,stale=False):
    slot=utcslot(now)
    ts=int((now-timedelta(seconds=2 if not stale else 200)).timestamp()*1000)
    books=[
        {"market":market,"timestamp":ts,"level":0,
         "orderbook_units":[{"ask_price":101.+i,"bid_price":99.-i,
                             "ask_size":40.,"bid_size":25.}
                            for i in range(30)]}
        for market in ASSETS
    ]
    raw=json.dumps(books).encode()
    counter={"books":0,"trades":0}
    def book_getter():
        counter["books"]+=1
        return {"requested_utc":(now-timedelta(seconds=.2)).isoformat(),
                "received_utc":now.isoformat(),"request_rtt_seconds":.2,
                "raw_payload_utf8":raw.decode(),"raw_payload_sha256":sha256(raw).hexdigest(),
                "books":books}
    def trade_getter(market):
        counter["trades"]+=1
        if market==tape_issue:
            raise RuntimeError("API degraded")
        rec=[{"market":market,"timestamp":ts-i*1000,
              "trade_price":100.,"trade_volume":.1,
              "sequential_id":1000+i,"ask_bid":"BID" if i%2==0 else "ASK"}
             for i in range(3)]
        tape=json.dumps(rec).encode()
        return {"market":market,"requested_utc":now.isoformat(),
                "received_utc":(now+timedelta(seconds=1)).isoformat(),
                "raw_sha256":sha256(tape).hexdigest(),"raw":tape,
                "quality":{"trades":len(rec)}}
    return book_getter,trade_getter,counter


def do(db,now,**kwargs):
    b,t,count=fake_sources(now,**kwargs)
    result=capture_once(db,clock=lambda:now,book_getter=b,trade_getter=t)
    return result,count


def test_atomic_all_five_payloads_and_stable_idempotency(tmp_path):
    path=tmp_path/"observation.sqlite3"
    now=clock_at(3_000_000)
    res,count=do(path,now)
    assert res["status"]=="CAPTURED_ATOMICALLY"
    assert res["public_trades"]==12
    assert count=={"books":1,"trades":4}
    again,counter=do(path,now)
    assert again["status"]=="ALREADY_CAPTURED"
    assert counter=={"books":0,"trades":0}
    with _db(path) as c:
        assert c.execute("SELECT count(*) FROM observation").fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM market_trades").fetchone()[0]==4
    audited=inspect(path,now=now)
    assert audited["bundles"]==1
    assert audited["four_asset_orderbooks"]==4
    assert audited["sampled_public_trade_prints"]==12
    assert len(audited["depth_sweep_summaries"])==4*2*4
    assert audited["realized_order_fills_observed"]==0
    assert audited["status"]=="INSUFFICIENT_PROSPECTIVE_DEPTH_COVERAGE"
    assert not audited["real_orders_enabled"]


def test_partial_network_failure_never_commits_incomplete_bundle(tmp_path):
    path=tmp_path/"archive.db"
    now=clock_at(3_000_010)
    b,t,_=fake_sources(now,tape_issue="KRW-XRP")
    with pytest.raises(RuntimeError,match="degraded"):
        capture_once(path,clock=lambda:now,book_getter=b,trade_getter=t)
    with _db(path) as c:
        assert c.execute("SELECT count(*) FROM observation").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM market_trades").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM capture_failures").fetchone()[0]==1
    res,_=do(path,now)
    assert res["status"]=="CAPTURED_ATOMICALLY"
    audit=inspect(path,now=now)
    assert audit["capture_failures"]==1 and audit["bundles"]==1


def test_stale_quote_rejected_and_failure_persisted(tmp_path):
    path=tmp_path/"fail.db"
    now=clock_at(3_000_050)
    b,t,_=fake_sources(now,stale=True)
    with pytest.raises(ValueError,match="Stale"):
        capture_once(path,clock=lambda:now,book_getter=b,trade_getter=t)
    audit=inspect(path,now=now)
    assert audit["bundles"]==0
    assert audit["capture_failures"]==1


def test_missing_offline_slots_are_not_synthesized_or_duplicated(tmp_path):
    path=tmp_path/"gaps.db"
    first=clock_at(3_010_000)
    do(path,first)
    second=clock_at(3_010_004)
    do(path,second)
    audit=inspect(path,now=second)
    assert audit["bundles"]==2
    assert audit["gaps_since_first_sample_slots"]==3
    assert audit["first_20_missing_slots"]==[3010001,3010002,3010003]
    assert audit["observed_schedule_coverage"]==pytest.approx(2/5)
    with _db(path) as c:
        assert c.execute("SELECT count(*) FROM observation").fetchone()[0]==2


def test_tampered_raw_books_or_tape_refused_on_audit(tmp_path):
    path=tmp_path/"raw.db"
    now=clock_at(3_020_000)
    do(path,now)
    with _db(path) as c:
        c.execute("UPDATE market_trades SET raw_payload=? WHERE market='KRW-XRP'",
                  (b'["tampered"]',))
    with pytest.raises(ValueError,match="Tampered trade"):
        inspect(path,now=now)
    path2=tmp_path/"raw2.db"
    do(path2,now)
    with _db(path2) as c:
        c.execute("UPDATE observation SET book_raw=?", (b'[]',))
    with pytest.raises(ValueError,match="Tampered market orderbook"):
        inspect(path2,now=now)


def test_nearby_trade_only_descriptive_not_customer_fills(tmp_path):
    path=tmp_path/"join.db"
    now=clock_at(3_040_000)
    do(path,now)
    a=inspect(path,now=now)
    aligned=a["quote_trade_temporal_proximity_records"]
    assert len(aligned)==4
    assert all(x["within_5s_descriptive_only"] for x in aligned)
    assert all(x["no_proof_of_same_order_or_executable_quote"] for x in aligned)
    assert a["orderbook_trade_are_asynchronous"]
    assert a["sampled_public_trade_prints"]==12


def test_declared_24h_gate_does_not_claim_actual_fill_accuracy(tmp_path):
    path=tmp_path/"many.db"
    start=3_100_000
    for ix in range(OBSERVATION_GATE_SAMPLES):
        do(path,clock_at(start+ix*2))
    last=clock_at(start+2*(OBSERVATION_GATE_SAMPLES-1))
    a=inspect(path,now=last)
    assert a["observation_span_hours"]>24
    assert a["minimum_24_hour_100_slot_gate"]
    assert a["gaps_since_first_sample_slots"]==OBSERVATION_GATE_SAMPLES-1
    assert a["status"]=="INSUFFICIENT_PROSPECTIVE_DEPTH_COVERAGE"
    assert a["realized_order_fills_observed"]==0
    assert not a["automatic_strategy_promotion"]


def test_trade_id_dedup_across_two_slots_and_interval_truncation_flag(tmp_path):
    path=tmp_path/"two.db"
    first=clock_at(3_050_000)
    second=clock_at(3_050_001)
    do(path,first)
    do(path,second)
    report=inspect(path,now=second)
    assert report["sampled_public_trade_prints"]==24
    # Both deliberately mocked slots reuse trade IDs 1000,1001,1002.
    assert report["unique_public_trade_ids_across_samples"]==12
    assert len(report["time_ordered_tape_interval_observations"])==8
    assert report["same_timestamp_fill_matching_not_proven"]
    assert not report["minimum_24_hour_100_slot_gate"]


def test_stale_collector_freshness_alert_does_not_fake_backfill(tmp_path):
    path=tmp_path/"health.db"
    start=clock_at(3_060_000)
    do(path,start)
    assert inspect(path,now=start)["collector_fresh_within_30_minutes"]
    late=start+timedelta(minutes=40)
    r=inspect(path,now=late)
    assert not r["collector_fresh_within_30_minutes"]
    assert r["gaps_since_first_sample_slots"]==4
    assert r["bundles"]==1

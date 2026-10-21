"""Periodic, restart-safe, PUBLIC-only Upbit orderbook + trade observation.

Each 10-minute UTC slot is at most one committed evidence bundle. Atomic
SQLite transactions seal one fresh four-market L2 book and four public trade
print windows. Trade lists are retrieved AFTER the book; quote/prints are
asynchronous, not proven synchronized or causal counterfactual executions.

Never attempts Exchange endpoints, private credentials or live orders.
Outages are permanently observable as *missing slots*, NEVER backfilled by
invented historical L2. Failures do not commit half-valid bundles.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import statistics
import time

import numpy as np
import requests

from .continuous_portfolio import ASSETS
from .daily_receipt import _safe_report
from .live_orderbook_evidence import (
    _check_book, _utc, fetch_public_snapshot, sweep, ORDER_NOTIONALS,
)
from .live_trade_evidence import URL as TRADES_URL, COUNT, check_trade_prints

CADENCE_SECONDS=600
MAX_PAIR_RECEIPT_SKEW_SECONDS=35.
OBSERVATION_GATE_SAMPLES=100
OBSERVATION_GATE_SPAN_SECONDS=24*3600
SCHEMA=1


def utcnow():
    return datetime.now(timezone.utc)


def utcslot(now:datetime)->int:
    return int(_utc(now).timestamp())//CADENCE_SECONDS


def sha(data:bytes)->str:
    return sha256(data).hexdigest()


def _db(path:str|Path)->sqlite3.Connection:
    target=Path(path)
    target.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(target),timeout=10)
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS observation (
      slot INTEGER PRIMARY KEY,
      book_requested_utc TEXT NOT NULL,
      book_received_utc TEXT NOT NULL,
      last_trade_received_utc TEXT NOT NULL,
      book_sha256 TEXT NOT NULL,
      book_raw BLOB NOT NULL,
      book_request_rtt REAL NOT NULL CHECK (book_request_rtt>=0),
      created_at_utc TEXT NOT NULL,
      source TEXT NOT NULL CHECK (source='public_upbit_rest'),
      schema_version INTEGER NOT NULL CHECK (schema_version=1)
    );
    CREATE TABLE IF NOT EXISTS market_trades (
      slot INTEGER NOT NULL REFERENCES observation(slot) ON DELETE RESTRICT,
      market TEXT NOT NULL,
      requested_utc TEXT NOT NULL,
      received_utc TEXT NOT NULL,
      raw_sha256 TEXT NOT NULL,
      raw_payload BLOB NOT NULL,
      PRIMARY KEY (slot, market)
    );
    CREATE TABLE IF NOT EXISTS capture_failures (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      slot INTEGER NOT NULL,
      failed_at_utc TEXT NOT NULL,
      reason TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_capture_failures_slot ON capture_failures(slot);
    """)
    return c


def _read_trades(market:str,*,session=requests,clock=utcnow)->dict:
    if market not in ASSETS:
        raise ValueError("Unknown non-research asset")
    started=_utc(clock())
    response=session.get(
        TRADES_URL,params={"market":market,"count":COUNT},
        headers={"Accept":"application/json",
                 "User-Agent":"FreeQuantLab/0.2 public observation readonly"},
        timeout=(3,7),
    )
    received=_utc(clock())
    response.raise_for_status()
    if (received-started).total_seconds()>8:
        raise ValueError("Recent public trade request too slow")
    raw=response.content
    if not raw or len(raw)>250_000:
        raise ValueError("Invalid public trade response bytes")
    parsed=json.loads(raw)
    quality=check_trade_prints(parsed,market,received)
    return {"market":market,"requested_utc":started.isoformat(),
            "received_utc":received.isoformat(),"raw":raw,
            "raw_sha256":sha(raw),"quality":quality}


def capture_once(
    db_path:str|Path, *,
    clock=utcnow,
    book_getter=fetch_public_snapshot,
    trade_getter=_read_trades,
)->dict:
    now=_utc(clock())
    slot=utcslot(now)
    c=_db(db_path)
    try:
        if c.execute("SELECT 1 FROM observation WHERE slot=?",(slot,)).fetchone():
            return {"status":"ALREADY_CAPTURED","slot":slot,
                    "new_bundles":0,"real_orders_enabled":False}
        try:
            book=book_getter()
            brecv=_utc(book["received_utc"])
            body=book["raw_payload_utf8"].encode("utf-8")
            if sha(body)!=book["raw_payload_sha256"]:
                raise ValueError("Public orderbook raw SHA mismatch")
            quotes=json.loads(body)
            if not isinstance(quotes,list) or len(quotes)!=len(ASSETS):
                raise ValueError("Not four public orderbook markets")
            checked=[_check_book(x,brecv) for x in quotes]
            if set(x["market"] for x in checked)!=set(ASSETS):
                raise ValueError("Duplicated/missing market book")
            if utcslot(brecv)!=slot:
                raise ValueError("Book capture fell outside scheduled UTC slot")
            packets=[]
            for market in ASSETS:
                evidence=trade_getter(market)
                if evidence["market"]!=market or sha(evidence["raw"])!=evidence["raw_sha256"]:
                    raise ValueError("Trade receipt source/sha mismatch")
                rcv=_utc(evidence["received_utc"])
                if rcv < brecv or (rcv-brecv).total_seconds()>MAX_PAIR_RECEIPT_SKEW_SECONDS:
                    raise ValueError("Quote-to-trade requests not in same short collection window")
                parsed=json.loads(evidence["raw"])
                check_trade_prints(parsed,market,rcv)
                packets.append(evidence)
            # Only after every source passes do we commit ALL FIVE JSON
            # payloads. Duplicate slot (another process raced us) is ignored.
            with c:
                done=c.execute(
                    """INSERT OR IGNORE INTO observation
                    (slot, book_requested_utc,book_received_utc,
                     last_trade_received_utc,book_sha256,book_raw,
                     book_request_rtt,created_at_utc,source,schema_version)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (slot,book["requested_utc"],book["received_utc"],
                     packets[-1]["received_utc"],sha(body),body,
                     book["request_rtt_seconds"],_utc(clock()).isoformat(),
                     "public_upbit_rest",SCHEMA),
                )
                if not done.rowcount:
                    return {"status":"ALREADY_CAPTURED","slot":slot,
                            "new_bundles":0,"real_orders_enabled":False}
                for packet in packets:
                    c.execute(
                        """INSERT INTO market_trades
                        (slot,market,requested_utc,received_utc,raw_sha256,raw_payload)
                        VALUES(?,?,?,?,?,?)""",
                        (slot,packet["market"],packet["requested_utc"],
                         packet["received_utc"],packet["raw_sha256"],packet["raw"]),
                    )
            return {"status":"CAPTURED_ATOMICALLY","slot":slot,
                    "new_bundles":1,"public_trades":sum(p["quality"]["trades"] for p in packets),
                    "markets":list(ASSETS),"real_orders_enabled":False}
        except Exception as exc:
            with c:
                c.execute(
                    "INSERT INTO capture_failures(slot,failed_at_utc,reason) VALUES(?,?,?)",
                    (slot,_utc(clock()).isoformat(),f"{type(exc).__name__}: {str(exc)[:350]}"),
                )
            raise
    finally:
        c.close()


def inspect(db_path:str|Path,*,now:datetime|None=None,
            lookback_hours:int|None=48)->dict:
    current=_utc(now or utcnow())
    if lookback_hours is not None and (
        type(lookback_hours)!=int or not 1<=lookback_hours<=24*31
    ):
        raise ValueError("Invalid bounded audit window")
    c=_db(db_path)
    try:
        all_slots=[r[0] for r in c.execute("SELECT slot FROM observation ORDER BY slot")]
        floor_slot=(utcslot(current)-(lookback_hours*3600//CADENCE_SECONDS)
                    if lookback_hours is not None else 0)
        cursor=c.execute(
            """SELECT slot,book_received_utc,book_sha256,book_raw,
                      last_trade_received_utc,book_request_rtt
               FROM observation WHERE slot >= ? ORDER BY slot""",
            (floor_slot,),
        )
        bundles=cursor.fetchall()
        failures=c.execute(
            "SELECT slot,reason,failed_at_utc FROM capture_failures ORDER BY id"
        ).fetchall()
        first=bundles[0][0] if bundles else None
        last=bundles[-1][0] if bundles else None
        # Stop at current time: never claim an unobserved future slot failed.
        final_slot=max(utcslot(current),last or utcslot(current))
        missing=(sorted(set(range(first,final_slot+1))-
                        {row[0] for row in bundles}) if first is not None else [])
        observations=[]
        trade_windows=[]
        tape_intervals=[]
        previous_quote_by_market={}
        unique_trade_ids_by_market={m:set() for m in ASSETS}
        for slot,received,h,raw,last_received,rtt in bundles:
            if sha(raw)!=h:
                raise ValueError(f"Tampered market orderbook raw SHA in slot {slot}")
            quotes=json.loads(raw)
            if len(quotes)!=len(ASSETS):
                raise ValueError("Invalid stored market count")
            verified=[_check_book(b,_utc(received)) for b in quotes]
            if set(b["market"] for b in verified)!=set(ASSETS):
                raise ValueError("Missing stored market")
            trades=c.execute(
                """SELECT market,requested_utc,received_utc,raw_sha256,raw_payload
                   FROM market_trades WHERE slot=? ORDER BY market""",
                (slot,),
            ).fetchall()
            if len(trades)!=len(ASSETS):
                raise ValueError("Partial atomic market tape bundle")
            for market,requested,trcv,th,tbody in trades:
                if sha(tbody)!=th:
                    raise ValueError(f"Tampered trade raw SHA for {market} in {slot}")
                prints=json.loads(tbody)
                q=check_trade_prints(prints,market,_utc(trcv))
                b=next(x for x in verified if x["market"]==market)
                nearest_delta_ms=min(abs(t["timestamp"]-b["timestamp_ms"])
                                     for t in prints)
                previous=previous_quote_by_market.get(market)
                interval=[t for t in prints if previous is not None and
                          previous<t["timestamp"]<=b["timestamp_ms"]]
                fresh=sum(t["sequential_id"] not in unique_trade_ids_by_market[market]
                          for t in prints)
                unique_trade_ids_by_market[market].update(t["sequential_id"] for t in prints)
                tape_intervals.append({
                    "slot":slot,"market":market,
                    "previous_book_ts_ms":previous,
                    "current_book_ts_ms":b["timestamp_ms"],
                    "prints_within_book_interval":len(interval),
                    "buy_aggressor_prints_in_interval":sum(
                        t["ask_bid"]=="BID" for t in interval),
                    "sell_aggressor_prints_in_interval":sum(
                        t["ask_bid"]=="ASK" for t in interval),
                    "new_trade_ids_vs_prior_samples":fresh,
                    "possible_200_trade_pagination_truncation":bool(
                        previous is not None and len(prints)==COUNT and
                        min(t["timestamp"] for t in prints)>previous),
                    "rest_200_trade_limit_may_leave_unknown_gaps":True,
                    "matching_is_interval_of_quotes_not_proven_fills":True,
                })
                # These trades are recorded after the book GET. The nearest
                # print in the recent 200 is only a temporal proximity
                # observation, not a validated execution / queue join.
                trade_windows.append({
                    "slot":slot,"market":market,
                    "sampled_trades":q["trades"],
                    "tape_window_seconds":q["trade_window_seconds"],
                    "latest_age_at_receipt_seconds":q["latest_age_seconds"],
                    "nearest_print_to_quote_ms":nearest_delta_ms,
                    "within_5s_descriptive_only":nearest_delta_ms<=5000,
                    "book_exchange_timestamp_ms":b["timestamp_ms"],
                    "tape_received_utc":trcv,
                    "no_proof_of_same_order_or_executable_quote":True,
                })
            for book in verified:
                previous_quote_by_market[book["market"]]=book["timestamp_ms"]
                for side in ("BUY","SELL"):
                    for size in ORDER_NOTIONALS:
                        outcome=sweep(book,side,size)
                        observations.append({
                            "slot":slot,"market":book["market"],
                            "quote_exchange_timestamp_ms":book["timestamp_ms"],
                            "side":side,"order_notional_krw":size,
                            "spread_bps":(book["best_ask"]-book["best_bid"])*20000/
                                          (book["best_ask"]+book["best_bid"]),
                            "book_age_seconds":book["age_seconds"],
                            **outcome,
                        })
        aggregates=[]
        for asset in ASSETS:
            for side in ("BUY","SELL"):
                for size in ORDER_NOTIONALS:
                    subset=[x for x in observations if x["market"]==asset and
                            x["side"]==side and x["order_notional_krw"]==size]
                    complete=[x["full_fill_vs_mid_bps"] for x in subset
                              if x["complete_visible_depth"]]
                    aggregates.append({
                        "market":asset,"side":side,"order_notional_krw":size,
                        "samples":len(subset),
                        "book_full_depth_count":len(complete),
                        "insufficient_displayed_depth_count":len(subset)-len(complete),
                        "median_visible_sweep_bps":float(np.median(complete)) if complete else None,
                        "p90_visible_sweep_bps":float(np.percentile(complete,90)) if complete else None,
                    })
        n=len(bundles)
        span=( _utc(bundles[-1][1])-_utc(bundles[0][1]) ).total_seconds() if n>=2 else 0.
        valid_sample=n>=OBSERVATION_GATE_SAMPLES and span>=OBSERVATION_GATE_SPAN_SECONDS
        coverage=n/(n+len(missing)) if n+len(missing)>0 else 0.
        recent_age=(current-_utc(bundles[-1][1])).total_seconds() if n else None
        # Even 24-hour coverage is NOT calibrated real fills.
        return {
            "status":("DEPTH_COVERAGE_OBSERVED_NOT_REAL_FILL_CALIBRATION"
                      if valid_sample and coverage>=.9 else
                      "INSUFFICIENT_PROSPECTIVE_DEPTH_COVERAGE"),
            "db_path":str(Path(db_path)),
            "detailed_audit_lookback_hours":lookback_hours,
            "total_archived_slots":len(all_slots),
            "total_historical_gap_slots":sum(max(0,b-a-1) for a,b in zip(all_slots,all_slots[1:])),
            "first_slot":first,"latest_slot":last,
            "bundles":n,"four_asset_orderbooks":n*4,
            "sampled_public_trade_prints":sum(t["sampled_trades"] for t in trade_windows),
            "unique_public_trade_ids_across_samples":sum(len(ids) for ids in unique_trade_ids_by_market.values()),
            "tape_intervals_possible_truncation":sum(int(t["possible_200_trade_pagination_truncation"]) for t in tape_intervals),
            "time_ordered_tape_interval_observations":tape_intervals,
            "observation_span_hours":span/3600.,
            "time_since_latest_bundle_seconds":recent_age,
            "collector_fresh_within_30_minutes":bool(
                recent_age is not None and 0<=recent_age<=1800),
            "gaps_since_first_sample_slots":len(missing),
            "first_20_missing_slots":missing[:20],
            "capture_failures":len(failures),
            "recent_capture_failures":[
                {"slot":f[0],"reason":f[1],"time":f[2]} for f in failures[-10:]
            ],
            "observed_schedule_coverage":coverage,
            "minimum_24_hour_100_slot_gate":valid_sample,
            "quote_trade_temporal_proximity_records":trade_windows,
            "depth_sweep_summaries":aggregates,
            "realized_order_fills_observed":0,
            "orderbook_trade_are_asynchronous":True,
            "same_timestamp_fill_matching_not_proven":True,
            "duplicate_prints_across_snapshots_possible":True,
            "does_not_justify_retroactive_backtest_repricing":True,
            "cannot_backfill_offline_L2":True,
            "real_orders_enabled":False,
            "automatic_strategy_promotion":False,
        }
    finally:
        c.close()


def main(argv=None)->int:
    ap=argparse.ArgumentParser(description="Public-only periodic Upbit market evidence")
    ap.add_argument("--db",default="state/microstructure/observations.sqlite3")
    ap.add_argument("--audit",action="store_true")
    ap.add_argument("--require-fresh-seconds",type=int,default=None)
    ap.add_argument("--full-history",action="store_true")
    ap.add_argument("--report",default=None)
    args=ap.parse_args(argv)
    if args.audit:
        result=inspect(args.db,lookback_hours=None if args.full_history else 48)
        if args.report is not None:
            path=Path(args.report)
            if path.exists():
                raise ValueError("Append-only audit cannot overwrite")
            path.parent.mkdir(parents=True,exist_ok=True)
            _safe_report(path,result)
        print(json.dumps({k:v for k,v in result.items() if k not in (
            "quote_trade_temporal_proximity_records","depth_sweep_summaries",
            "time_ordered_tape_interval_observations"
        )},ensure_ascii=False))
        if args.require_fresh_seconds is not None:
            if args.require_fresh_seconds<=0:
                raise ValueError("Freshness SLA must be positive")
            age=result["time_since_latest_bundle_seconds"]
            if age is None or age<0 or age>args.require_fresh_seconds:
                return 2  # journal-visible watchdog failure, not a fake success
        return 0
    report=capture_once(args.db)
    print(json.dumps(report,ensure_ascii=False))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

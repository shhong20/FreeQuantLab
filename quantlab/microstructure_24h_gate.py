"""Independent rolling 24h evidence gate; never confuses L2 with real fills.

All market evidence is checked through inspect() (raw SHA, quote timestamps,
four raw trade tapes, and original source provenance). The rolling success
gate uses only UTC 10-minute *due* slots. No missing L2 is backfilled,
no partially collected slot is counted, and 200-trade windows are not treated
as complete market tape.

Passing this gate means a usable 24h sequence of visible L2 snapshots, not
reliable liquidity calibration, queue priority or execution proof.
"""
from __future__ import annotations

from datetime import datetime,timezone
from pathlib import Path
import json
import sqlite3

from .continuous_portfolio import ASSETS
from .microstructure_observer import (
    CADENCE_SECONDS, inspect, utcnow, utcslot, _utc,
)

EXPECTED_DAILY_SLOTS=24*3600//CADENCE_SECONDS
MIN_UNIQUE_BOOK_TIMESTAMPS=100
MIN_WINDOW_COVERAGE=.90
MAX_CONSECUTIVE_MISSING_SLOTS=3
AUDIT_GRACE_SECONDS=120
MAX_FRESHNESS_SECONDS=1800


def _max_consecutive(seq:list[int])->int:
    longest=current=0
    for missed in seq:
        if missed:
            current+=1
            longest=max(longest,current)
        else:
            current=0
    return longest


def assess_rolling_24h(db_path:str|Path,*,now:datetime|None=None)->dict:
    now=_utc(now or utcnow())
    # The slot scheduled for +:30s is considered due only after a 2-minute
    # grace to avoid flagging the collector while its REST calls are in flight.
    latest_due=int((now.timestamp()-AUDIT_GRACE_SECONDS)//CADENCE_SECONDS)
    first_due=latest_due-EXPECTED_DAILY_SLOTS+1
    validated=inspect(db_path,now=now,lookback_hours=48)
    con=sqlite3.connect("file:"+str(Path(db_path).resolve())+"?mode=ro",uri=True)
    try:
        start,first_archive_slot=con.execute(
            "SELECT min(book_received_utc),min(slot) FROM observation"
        ).fetchone()
        records=list(con.execute(
            "SELECT slot,book_raw FROM observation WHERE slot BETWEEN ? AND ? ORDER BY slot",
            (first_due,latest_due),
        ))
        slots=[s for s,_ in records]
    finally:
        con.close()
    first_at=_utc(start) if start is not None else None
    elapsed=(now-first_at).total_seconds() if first_at is not None else 0.
    span_ready=elapsed>=24*3600
    actual=set(slots)
    missing=[s for s in range(first_due,latest_due+1) if s not in actual]
    if len(actual)+len(missing)!=EXPECTED_DAILY_SLOTS:
        raise ValueError("Rolling observation slot arithmetic invalid")
    coverage=len(actual)/EXPECTED_DAILY_SLOTS
    # Missing slots from before the collector was first installed are NOT
    # operational outages. Keep them separate until a full day has elapsed.
    since_start=max(first_due,first_archive_slot) if first_archive_slot is not None else latest_due+1
    operational_due=max(0,latest_due-since_start+1)
    operational_missing=sum(s not in actual for s in range(since_start,latest_due+1))
    operational_coverage=((operational_due-operational_missing)/operational_due
                          if operational_due else 0.)
    pre_start_not_observable=max(0,min(EXPECTED_DAILY_SLOTS,since_start-first_due))
    longest=_max_consecutive([s not in actual for s in range(first_due,latest_due+1)])
    seen={asset:set() for asset in ASSETS}
    for slot,raw in records:
        for book in json.loads(raw):
            seen[book["market"]].add(book["timestamp"])
    per_asset={}
    for asset in ASSETS:
        uniq=seen[asset]
        per_asset[asset]={
            "distinct_verified_exchange_book_timestamps":len(uniq),
            "minimum_100_independent_timestamps":len(uniq)>=MIN_UNIQUE_BOOK_TIMESTAMPS,
        }
    recency=validated["time_since_latest_bundle_seconds"]
    fresh=recency is not None and 0<=recency<=MAX_FRESHNESS_SECONDS
    satisfied=(
        span_ready and coverage>=MIN_WINDOW_COVERAGE
        and longest<=MAX_CONSECUTIVE_MISSING_SLOTS
        and fresh
        and all(v["minimum_100_independent_timestamps"] for v in per_asset.values())
    )
    if not span_ready:
        status="COLLECTING_NOT_YET_24_HOURS"
    elif satisfied:
        status="24H_VISIBLE_L2_COVERAGE_PASS_NOT_REAL_FILL_CALIBRATION"
    else:
        status="24H_VISIBLE_L2_COVERAGE_DEGRADED"
    return {
        "status":status,
        "window_start_utc":datetime.fromtimestamp(
            first_due*CADENCE_SECONDS,timezone.utc).isoformat(),
        "window_end_due_slot_utc":datetime.fromtimestamp(
            latest_due*CADENCE_SECONDS,timezone.utc).isoformat(),
        "reference_now_utc":now.isoformat(),
        "age_of_archive_hours":elapsed/3600.,
        "elapsed_24h_criterion_met":span_ready,
        "expected_10min_slots":EXPECTED_DAILY_SLOTS,
        "validated_collected_slots_in_window":len(actual),
        "missing_due_slots":len(missing),
        "pre_installation_slots_not_observed":pre_start_not_observable,
        "operational_due_slots_since_first_capture":operational_due,
        "missed_operational_due_slots":operational_missing,
        "observed_operational_slot_coverage":operational_coverage,
        "first_15_missing_due_slot_ids":missing[:15],
        "longest_missing_run_slots":longest,
        "slot_coverage_fraction":coverage,
        "all_four_asset_depth_quality":per_asset,
        "recent_data_fresh":fresh,
        "last_success_age_seconds":recency,
        "observation_gate_passed":satisfied,
        "tape_records_may_repeat_or_be_truncated":True,
        "tape_truncated_windows_in_last_48h":validated["tape_intervals_possible_truncation"],
        "raw_SHA_and_four_source_bundle_verification":"PASS",
        "full_book_not_full_private_execution_queue":True,
        "book_trade_requests_asynchronous":True,
        "original_account_PnL_modified":False,
        "model_calibration_allowed":False,
        "real_order_fill_accuracy_verified":False,
        "real_orders_enabled":False,
        "automatic_strategy_promotion":False,
    }


def main(argv=None)->int:
    import argparse
    import json
    ap=argparse.ArgumentParser(description="Rolling 24-hour read-only quote data-quality gate")
    ap.add_argument("--db",default="/home/hong/apps/FreeQuantLab/state/microstructure/observations.sqlite3")
    ap.add_argument("--enforce-after-24h",action="store_true",
                    help="Fail watchdog after 24h if the independent 24h L2 gate is degraded")
    args=ap.parse_args(argv)
    result=assess_rolling_24h(args.db)
    print(json.dumps(result,ensure_ascii=False))
    if args.enforce_after_24h and result["elapsed_24h_criterion_met"] and not result["observation_gate_passed"]:
        return 2
    return 0


if __name__=="__main__":
    raise SystemExit(main())

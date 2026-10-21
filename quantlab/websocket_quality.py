"""Bounded read-only quality gate for periodic PUBLIC Upbit WebSocket samples.

SQL-only metrics measure completeness, timestamp ages, gaps and disk use.
Raw frame SHA/causality audits remain a separate, bounded checkpoint. A
coverage PASS is not evidence of broker fills, queue priority or impact.

No historical quote extrapolation; sampled 30s/10m windows are >=95% gaps.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime,timezone
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time

from .continuous_portfolio import ASSETS

PERIOD_SECONDS=600
SCHEDULE_OFFSET_SECONDS=120
GRACE_SECONDS=90
WINDOW_SECONDS=30
WINDOWS_PER_DAY=144
MIN_DAYS=1
MIN_BOOK_TIMESTAMPS=100
MIN_SLOT_SUCCESS=.90
MAX_CONSECUTIVE_MISSING=3
MAX_RECENT_AGE_SECONDS=2400
WARNING_DAILY_DISK_BYTES=1024**3
MAX_REASONABLE_DISK_BYTES=10*1024**3


def _p(xs,percentile):
    if not xs:
        return None
    values=sorted(xs)
    rank=(len(values)-1)*percentile/100
    lo=int(rank);hi=min(lo+1,len(values)-1)
    return float(values[lo]+(values[hi]-values[lo])*(rank-lo))


def _longest_consecutive_missing(slots):
    if not slots:
        return 0
    length=best=0
    for present in slots:
        length=(0 if present else length+1)
        best=max(length,best)
    return best


def evaluate_quality(db_path:str|Path,*,now_ms:int|None=None)->dict:
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    if type(now_ms) is not int or now_ms<1_500_000_000_000:
        raise ValueError("Invalid observation wall clock")
    path=Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    conn=sqlite3.connect("file:"+str(path)+"?mode=ro",uri=True)
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("Observation SQLite integrity failure")
        first=conn.execute("SELECT MIN(started_ms) FROM sessions").fetchone()[0]
        start_24h=now_ms-24*3600*1000
        started_utc=(datetime.fromtimestamp(first/1000,timezone.utc).isoformat()
                     if first is not None else None)
        past_24=conn.execute(
            """SELECT id,started_ms,finished_ms,status,intended_seconds,
                      frame_count,rejected_frames,duplicated_trade_ids,reconnects
               FROM sessions WHERE started_ms>=? AND started_ms<=?
               ORDER BY started_ms""",
            (start_24h,now_ms),
        ).fetchall()
        latest_completed=conn.execute(
            """SELECT MAX(finished_ms) FROM sessions
               WHERE status='BOUNDED_CAPTURE_COMPLETE'"""
        ).fetchone()[0]
        recent=(now_ms-latest_completed)/1000. if latest_completed else None
        due_slot=int((now_ms/1000-SCHEDULE_OFFSET_SECONDS-GRACE_SECONDS)//PERIOD_SECONDS)
        first_due=due_slot-WINDOWS_PER_DAY+1
        actual_slots=defaultdict(list)
        for session_id,started,finished,status,intended,count,rejected,duplicates,reconnects in past_24:
            if (finished is not None and
                finished<started) or intended<=0 or count<0:
                raise ValueError("Corrupt WebSocket session accounting")
            slot=int(started/1000)//PERIOD_SECONDS
            actual_slots[slot].append((started,finished,status,count,rejected,duplicates,reconnects))
        # Warm-up/manual extra capture is NOT an independent scheduled slot.
        counted=[s for s in range(first_due,due_slot+1)
                 if any(x[2]=="BOUNDED_CAPTURE_COMPLETE" for x in actual_slots.get(s,[]))]
        covered=len(counted)
        presence=[s in counted for s in range(first_due,due_slot+1)]
        missing=[s for s in range(first_due,due_slot+1) if s not in counted]
        age=(now_ms-first)/3600000. if first else 0.
        age_ready=bool(first is not None and age>=24.)
        # The first sample may have been a manually forced bootstrap, so
        # distinguish historical non-observation before first actual session.
        preexisting_slots=(sum(s*PERIOD_SECONDS*1000<first for s in missing)
                           if first else len(missing))
        ops_misses=len(missing)-preexisting_slots

        collected_seconds=sum(
            max(0,(finished-started)/1000.)
            for _,started,finished,_,_,_,_,_,_ in past_24
            if finished is not None
        )
        successful=[s for s in past_24 if s[3]=="BOUNDED_CAPTURE_COMPLETE"]
        failed=[s for s in past_24 if s[3]!="BOUNDED_CAPTURE_COMPLETE"]
        sample_ids={s[0] for s in past_24}
        pairs={x:{"books":0,"trades":0,"linked_trades":0,
                  "book_exchange_timestamps":set(),
                  "book_age_ms":[],"trade_age_ms":[]} for x in ASSETS}
        bytes_raw=0
        frames_count=0
        # This query reads only indexed metadata, never raw JSON, to avoid
        # quadratic 48h IO/memory. A separate SHA audit validates raw bytes.
        for session,kind,market,receipt,exchange,link,raw_len in conn.execute(
            """SELECT f.session_id,f.kind,f.market,f.receipt_ms,f.exchange_ms,
                      f.link_status,length(f.raw)
               FROM frames f JOIN sessions s ON s.id=f.session_id
               WHERE s.started_ms BETWEEN ? AND ? ORDER BY f.seq""",
            (start_24h,now_ms),
        ):
            if market not in pairs or kind not in ("trade","orderbook"):
                raise ValueError("Unexpected stored public market frame")
            if session not in sample_ids or raw_len is None or raw_len<=0:
                raise ValueError("Incomplete stored WebSocket raw metadata")
            frames_count+=1
            bytes_raw+=raw_len
            ref=pairs[market]
            lag=receipt-exchange
            if kind=="orderbook":
                ref["books"]+=1
                ref["book_exchange_timestamps"].add(exchange)
                ref["book_age_ms"].append(lag)
            else:
                ref["trades"]+=1
                ref["trade_age_ms"].append(lag)
                if link=="CAUSAL_VISIBLE_L2_REFERENCE_ONLY":
                    ref["linked_trades"]+=1
        if frames_count!=sum(s[5] for s in past_24):
            raise ValueError("WebSocket frame-count mismatch")
        per_market={}
        for name,group in pairs.items():
            per_market[name]={
                "book_frames":group["books"],
                "unique_book_exchange_timestamps":len(group["book_exchange_timestamps"]),
                "market_trade_prints":group["trades"],
                "causal_earlier_book_trade_references":group["linked_trades"],
                "book_receipt_minus_exchange_ms_p50":_p(group["book_age_ms"],50),
                "book_receipt_minus_exchange_ms_p95":_p(group["book_age_ms"],95),
                "book_receipt_minus_exchange_ms_max":max(group["book_age_ms"],default=None),
                "trade_receipt_minus_exchange_ms_p95":_p(group["trade_age_ms"],95),
                "limited_market_print_sample":group["trades"]<100,
                "own_customer_fills":0,
            }
        clock_age_bad=any(
            x["book_receipt_minus_exchange_ms_p95"] is None or
            x["book_receipt_minus_exchange_ms_p95"]>3000 or
            x["book_receipt_minus_exchange_ms_p95"] < -3000
            for x in per_market.values()
        )
        coverage=covered/WINDOWS_PER_DAY
        longest=_longest_consecutive_missing(presence)
        mature_books=all(
            x["unique_book_exchange_timestamps"]>=MIN_BOOK_TIMESTAMPS
            for x in per_market.values()
        )
        fresh=recent is not None and 0<=recent<=MAX_RECENT_AGE_SECONDS
        healthy=(age_ready and coverage>=MIN_SLOT_SUCCESS and
                 longest<=MAX_CONSECUTIVE_MISSING and mature_books
                 and fresh and not clock_age_bad)
        if not age_ready:
            status="COLLECTING_NOT_YET_24_HOURS"
        else:
            status=("PROSPECTIVE_WINDOW_OBSERVATION_PASS_NOT_EXECUTION"
                    if healthy else "PROSPECTIVE_WINDOW_OBSERVATION_DEGRADED")

        db_bytes=sum(p.stat().st_size for p in (
            path,Path(str(path)+"-wal"),Path(str(path)+"-shm")
        ) if p.exists())
        n=len(past_24)
        # Forecast from observed bytes per session (disk overhead approximation),
        # NOT a guarantee of future utilization: traffic intensity varies.
        per_sess=db_bytes/n if n else 0
        projected_daily=per_sess*WINDOWS_PER_DAY if n else None
        rates=[(max(0,x[2]-x[1])/1000.) for x in past_24 if x[2] is not None]
        result={
            "status":status,
            "as_of_utc":datetime.fromtimestamp(now_ms/1000,timezone.utc).isoformat(),
            "first_real_session_utc":started_utc,
            "archive_age_hours":round(age,4),
            "rolling_24h_expected_scheduled_slots":WINDOWS_PER_DAY,
            "rolling_24h_successful_slot_count":covered,
            "rolling_24h_fraction":coverage,
            "rolling_24h_missing_due_slots":len(missing),
            "pre_installation_unobservable_slots":preexisting_slots,
            "operational_missing_due_slots":ops_misses,
            "max_consecutive_missing_due_slots":longest,
            "total_sessions_last_24h":n,
            "clean_sessions_last_24h":len(successful),
            "failed_degraded_sessions_last_24h":len(failed),
            "reconnections_last_24h":sum(x[8] for x in past_24),
            "rejected_frames_last_24h":sum(x[6] for x in past_24),
            "duplicate_trade_ids_last_24h":sum(x[7] for x in past_24),
            "seconds_since_last_clean_session":recent,
            "window_capture_duration_seconds_total":collected_seconds,
            "wall_clock_observation_duty_fraction_at_full_schedule":
                min(1,WINDOW_SECONDS/PERIOD_SECONDS),
            "window_duration_seconds_p50":_p(rates,50),
            "window_duration_seconds_p95":_p(rates,95),
            "raw_frame_payload_bytes_last_24h":bytes_raw,
            "disk_db_with_wal_shm_bytes":db_bytes,
            "rough_forecast_daily_db_bytes_if_current_samples_repeat":projected_daily,
            "rough_forecast_14d_bytes":projected_daily*14 if projected_daily else None,
            "daily_disk_budget_warning":bool(
                projected_daily is not None and projected_daily>WARNING_DAILY_DISK_BYTES
            ),
            "actual_db_exceeds_10g_warning":db_bytes>MAX_REASONABLE_DISK_BYTES,
            "books_per_market_and_receipt_lag":per_market,
            "clock_age_p95_rule_passed":not clock_age_bad,
            "first_24h_available":age_ready,
            "provisional_24h_sample_gate_passed":healthy,
            "raw_cryptographic_audit_is_separate":True,
            "gaps_between_30s_windows_have_no_ws_evidence":True,
            "trade_prints_not_customer_fills":True,
            "retention_no_automatic_deletion":True,
            "model_calibration_allowed":False,
            "real_orders_enabled":False,
            "automatic_model_promotion":False,
        }
        return result
    finally:
        conn.close()


def main(argv=None)->int:
    ap=argparse.ArgumentParser(description="Market WebSocket 24h quote/tape quality & storage guard")
    ap.add_argument("--db",default="/home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3")
    ap.add_argument("--enforce",action="store_true")
    ap.add_argument("--report",default=None)
    args=ap.parse_args(argv)
    report=evaluate_quality(args.db)
    if args.report:
        output=Path(args.report)
        if output.exists():
            raise ValueError("Refuse to overwrite archived quality report")
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(report,ensure_ascii=False))
    if args.enforce and (
        report["daily_disk_budget_warning"] or
        report["actual_db_exceeds_10g_warning"] or
        (report["first_24h_available"] and not report["provisional_24h_sample_gate_passed"])
    ):
        return 2
    return 0


if __name__=="__main__":
    raise SystemExit(main())

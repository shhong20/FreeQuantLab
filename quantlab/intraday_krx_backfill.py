"""Bounded 60-session KRX J historical capture planner and resumable batch runner.

Separate calendar planning from the old five-day request budget:
- plans use exchange_calendars XKRX as an indicative exchange timetable
- one --execute invocation is capped at five historical weekdays, J only
- original source snapshots are never overwritten, raw hashes rechecked
- progress lives outside the original source root
- no KIS credentials for planning/preview or already-complete batches
- no strategy selection, order submission, backtest or operating DB changes
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from .data import _kis_token
from .intraday_kis_cohort_capture import (
    DEFAULT_SYMBOLS, _comma_list, plan as capture_plan, run as capture_run,
)
from .intraday_kis_venue_compare import _read_one
from .intraday_kis_venues import VenueProbeError

KST = ZoneInfo("Asia/Seoul")
MAX_SESSION_COUNT = 120
BATCH_DAYS = 5
SCHEMA = "KIS_KRX_J_BOUNDED_60SESSION_V1"
# KRX explicitly closed on these two 2026 weekdays. exchange_calendars 4.13.2
# incorrectly emits both as sessions; excluding them is mandatory before
# generating a request plan or trusting a saved plan for historical capture.
KRX_2026_ANNOUNCED_CLOSURES = frozenset(("2026-06-03", "2026-07-17"))
KRX_2026_CLOSURE_NOTICE = "https://www.newspim.com/news/view/20260520000433"


def _json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)+"\n").encode()


def _calendar_days(*, end_session: str, session_count: int,
                   today: date | None = None) -> tuple[str, ...]:
    if not isinstance(end_session, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", end_session):
        raise VenueProbeError("Expected ISO end-session")
    try:
        end = date.fromisoformat(end_session)
    except ValueError as exc:
        raise VenueProbeError("Invalid end-session date") from exc
    current = today or datetime.now(KST).date()
    if not isinstance(session_count, int) or isinstance(session_count, bool) or not (
            1 <= session_count <= MAX_SESSION_COUNT):
        raise VenueProbeError("Bounded XKRX session count required")
    if end >= current:
        raise VenueProbeError("Only completed past trading sessions allowed")
    calendar=xcals.get_calendar("XKRX")
    # Wide prior period tolerates weekends and routine exchange holidays, but
    # is not proof that an ad-hoc closure is reflected in the library.
    left = (end-timedelta(days=max(210,session_count*3))).isoformat()
    try:
        days=calendar.sessions_in_range(left,end.isoformat())
    except (ValueError,OverflowError) as exc:
        raise VenueProbeError("XKRX exchange calendar does not cover dates") from exc
    values=tuple(str(d.date()) for d in days
                 if str(d.date()) not in KRX_2026_ANNOUNCED_CLOSURES)
    if not values or values[-1]!=end_session or len(values)<session_count:
        raise VenueProbeError("Requested end is not a verified XKRX calendar session")
    return values[-session_count:]


def create(*, plan_dir: str | Path, source_root: str | Path,
           end_session: str, session_count: int = 60,
           symbols: tuple[str,...] = ("005930","000660"),
           today: date | None = None) -> dict:
    days=_calendar_days(end_session=end_session,session_count=session_count,today=today)
    if len(set(symbols))!=len(symbols) or not 1<=len(symbols)<=3 or any(
            not isinstance(x,str) or not re.fullmatch(r"[0-9]{6}",x)
            for x in symbols):
        raise VenueProbeError("Invalid research symbol set")
    root=Path(source_root).expanduser().resolve()
    target=Path(plan_dir).expanduser().resolve()
    if target.exists():
        raise VenueProbeError("Immutable planning directory already exists")
    # The same historical capture plan is reused by every actual J-only
    # five-day batch; never relax the API budget or allow future dates.
    batches=[]
    for n,i in enumerate(range(0,len(days),BATCH_DAYS)):
        subset=days[i:i+BATCH_DAYS]
        tasks=capture_plan(symbols=",".join(symbols),
                           sessions=",".join(subset),markets="J",
                           today=today)
        batches.append({
            "index":n,
            "sessions":list(subset),
            "market":"J",
            "tasks":tasks,
        })
    existing=[]
    for session in days:
        for symbol in symbols:
            if (root/f"KIS_J_EXT_{symbol}_{session}").is_dir():
                audit,_=_read_one(root,symbol,session,"J")
                existing.append({
                    "symbol":symbol, "session":session,
                    "source_minute_bar_sha256":audit["source_minute_bar_sha256"],
                })
    manifest={
        "schema":SCHEMA, "venue":"J",
        "source_root":str(root), "symbols":list(symbols),
        "sessions":list(days),"target_sessions_per_symbol":len(days),
        "batch_days_max":BATCH_DAYS,"batches":batches,
        "existing_sources_at_plan_time":existing,
        "already_held_stock_day_snapshots":len(existing),
        "pending_stock_day_snapshots":len(days)*len(symbols)-len(existing),
        "calendar_name":"XKRX",
        "calendar_package_version":xcals.__version__,
        "krx_2026_announced_closure_dates":sorted(KRX_2026_ANNOUNCED_CLOSURES),
        "krx_2026_closure_notice_url":KRX_2026_CLOSURE_NOTICE,
        "calendar_is_schedule_candidate_not_official_trade_confirmation":True,
        "unexpected_holiday_or_provider_no_data_is_failure_not_zero_trading":True,
        "historical_source_timestamp_scope_verified":False,
        "auto_walkforward_enabled":False,
        "orders_enabled":False,
        "usable_for_backtesting":False,
    }
    blob=_json(manifest)
    target.mkdir(parents=True,exist_ok=False)
    (target/"plan.json").write_bytes(blob)
    (target/"plan.sha256").write_text(sha256(blob).hexdigest()+"\n",encoding="utf-8")
    return manifest


def load(plan_dir: str | Path) -> dict:
    target=Path(plan_dir).expanduser().resolve()
    try:
        raw=(target/"plan.json").read_bytes()
        expected=(target/"plan.sha256").read_text().strip()
        if sha256(raw).hexdigest()!=expected:
            raise VenueProbeError("Historical plan SHA mismatch")
        plan=json.loads(raw)
        if not isinstance(plan,dict) or plan.get("schema")!=SCHEMA or plan.get("venue")!="J":
            raise VenueProbeError("Historical plan contract mismatch")
        symbols=tuple(plan["symbols"])
        days=tuple(plan["sessions"])
        if any(day in KRX_2026_ANNOUNCED_CLOSURES for day in days):
            raise VenueProbeError("KRX announced closure in historical plan")
        if (not 1<=len(symbols)<=3 or len(set(symbols))!=len(symbols)
                or any(not isinstance(x,str) or not re.fullmatch(r"[0-9]{6}",x)
                       for x in symbols)
                or not 1<=len(days)<=MAX_SESSION_COUNT
                or len(set(days))!=len(days) or tuple(sorted(days))!=days):
            raise VenueProbeError("Unsafe or incoherent historical plan")
        # Verify a reconstituted, bounded batching contract. Never interpret
        # arbitrary JSON tasks as an authorization to issue API calls.
        batches=plan["batches"]
        if not isinstance(batches,list) or len(batches)!=(len(days)+4)//5:
            raise VenueProbeError("Historical batch sequence mismatch")
        for ix,batch in enumerate(batches):
            subset=days[ix*BATCH_DAYS:(ix+1)*BATCH_DAYS]
            reference=capture_plan(symbols=",".join(symbols),
                                   sessions=",".join(subset),markets="J")
            if not isinstance(batch,dict) or (
                    batch.get("index")!=ix or batch.get("market")!="J" or
                    batch.get("sessions")!=list(subset) or
                    batch.get("tasks")!=reference):
                raise VenueProbeError("Historical batch tasks mismatch")
        if plan.get("batch_days_max")!=5 or plan.get("target_sessions_per_symbol")!=len(days):
            raise VenueProbeError("Historical API bounds changed")
        if any(day in KRX_2026_ANNOUNCED_CLOSURES for day in days):
            raise VenueProbeError("KRX announced closure in historical plan")
        if days != _calendar_days(end_session=days[-1],session_count=len(days)):
            raise VenueProbeError("Historical plan sessions no longer match KRX calendar")
        root=Path(plan["source_root"]).expanduser().resolve()
        for row in plan["existing_sources_at_plan_time"]:
            if not isinstance(row,dict) or row.get("symbol") not in symbols or row.get("session") not in days:
                raise VenueProbeError("Historical existing baseline identity changed")
            audit,_=_read_one(root,row["symbol"],row["session"],"J")
            if audit["source_minute_bar_sha256"]!=row.get("source_minute_bar_sha256"):
                raise VenueProbeError("Historical existing baseline checksum changed")
        return plan
    except (OSError,ValueError,TypeError,KeyError,IndexError) as exc:
        if isinstance(exc,VenueProbeError):
            raise
        raise VenueProbeError("Cannot verify immutable historical capture plan") from exc


def run_batch(*, plan_dir: str | Path, batch_index: int, execute: bool = False,
              key: str = "", secret: str = "", token: str = "") -> dict:
    manifest=load(plan_dir)
    if isinstance(batch_index,bool) or not isinstance(batch_index,int) or not (
            0<=batch_index<len(manifest["batches"])):
        raise VenueProbeError("Batch index outside immutable plan")
    batch=manifest["batches"][batch_index]
    root=Path(manifest["source_root"])
    tasks=batch["tasks"]
    # Cheap provenance preflight. Do not request OAuth if all sources are
    # already present; do not overwrite old source files.
    preview=capture_run(tasks=tasks,root=root,execute=False)
    pending=[x for x in preview["tasks"] if x["status"]=="NOT_COLLECTED"]
    result={
        "status":"PREVIEW_ONLY" if not execute else "EXISTING_VERIFIED",
        "batch_index":batch_index,
        "sessions":batch["sessions"],
        "snapshot_count":len(tasks),
        "verified_existing":len(tasks)-len(pending),
        "pending":len(pending),
        "source_market":"J",
        "historical_calendar_provisional":True,
        "usable_for_backtesting":False,
        "orders_enabled":False,
    }
    if not execute or not pending:
        return result
    if not (key and secret and token):
        raise VenueProbeError("Local KIS credentials required for missing snapshots")
    plan_root=Path(plan_dir).expanduser().resolve()
    progress=plan_root/"progress"/f"batch_{batch_index:02d}"
    progress.mkdir(parents=True,exist_ok=True)
    # Prevent simultaneous requests in same batch. A crash may leave a lock:
    # an operator must review it, not silently steal a running session.
    lock=progress/".capture.lock"
    try:
        fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError as exc:
        raise VenueProbeError("Batch already executing or stale lock requires review") from exc
    try:
        os.close(fd)
        full=capture_run(tasks=tasks,root=root,progress_root=progress,
                         execute=True,key=key,secret=secret,token=token,
                         pacing_seconds=1.0,between_tasks_seconds=2.0,
                         max_failures=1)
    finally:
        lock.unlink(missing_ok=True)
    result.update({
        "status":full["status"],
        "http_get_requests":full["http_get_requests"],
        "verified_existing_or_new":full["completed_snapshot_count"],
        "failures":[
            {"symbol":t["symbol"],"session":t["session"],"error":t.get("error")}
            for t in full["tasks"] if t["status"]=="FAILED_CLOSED"
        ],
        "progress_report":str(progress/"batch_progress.json"),
    })
    return result


def run_all(*, plan_dir: str | Path, execute: bool = False,
            key: str = "", secret: str = "", token: str = "",
            pause: callable = time.sleep) -> dict:
    """All batches in order; explicit --execute only; stop at FIRST failure.

    Runs each batch under the existing five-day max. All KIS source folders
    are immutable, and each batch progress report is separate. A restart can
    resume without replaying previously verified HTTP requests.
    """
    manifest=load(plan_dir)
    previews=[
        run_batch(plan_dir=plan_dir,batch_index=i,execute=False)
        for i in range(len(manifest["batches"]))
    ]
    original_missing=sum(x["pending"] for x in previews)
    summary={
        "status":"ALL_BATCHES_PREVIEW_NO_NETWORK",
        "batch_count":len(previews),
        "remaining_before":original_missing,
        "source_market":"J",
        "usable_for_backtesting":False,
        "orders_enabled":False,
        "trading_calendar_provider_verified":False,
    }
    if not execute:
        return summary
    if original_missing and not (key and secret and token):
        raise VenueProbeError("Missing locally provided read-only KIS credentials")
    reports=[]
    previous_http=False
    for i in range(len(previews)):
        if previous_http:
            pause(2.0)  # delay across separate batch-local rate-limiters
        result=run_batch(plan_dir=plan_dir,batch_index=i,execute=True,
                         key=key,secret=secret,token=token)
        reports.append({
            "batch_index":i, "status":result["status"],
            "pending_before":result["pending"],
            "verified_existing_or_new":result.get(
                "verified_existing_or_new",result["verified_existing"]),
            "http_get_requests":result.get("http_get_requests",0),
        })
        print(json.dumps({"batch_progress":reports[-1]},sort_keys=True),flush=True)
        previous_http=result.get("http_get_requests",0)>0
        if result["status"] in ("STOPPED_ON_FAILURE_LIMIT","COMPLETED_WITH_FAILURES"):
            break
    # Do not mistake a successful request attempt for complete source
    # coverage. Revalidate every stored hash after the entire attempt.
    again=[
        run_batch(plan_dir=plan_dir,batch_index=i,execute=False)
        for i in range(len(manifest["batches"]))
    ]
    remaining=sum(x["pending"] for x in again)
    summary.update({
        "status":(
            "ALL_BATCHES_VERIFIED_COMPLETED" if not remaining else
            "ALL_BATCHES_PARTIALLY_COLLECTED_HOLD"
        ),
        "remaining_after":remaining,
        "snapshots_verified":len(manifest["sessions"])*len(manifest["symbols"])-remaining,
        "batch_reports":reports,
        "stop_on_first_provider_error":True,
    })
    return summary


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(
        description="Bounded XKRX 60-session historic J-only capture plan; dry run by default"
    )
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--init",action="store_true")
    mode.add_argument("--batch",type=int)
    mode.add_argument("--all-batches",action="store_true")
    parser.add_argument("--plan-dir",required=True)
    parser.add_argument("--source-root")
    parser.add_argument("--end-session")
    parser.add_argument("--session-count",type=int,default=60)
    parser.add_argument("--symbols",default=DEFAULT_SYMBOLS)
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args(argv)
    try:
        if args.init:
            if args.execute or not args.source_root or not args.end_session:
                raise VenueProbeError("--init requires root/end and forbids --execute")
            manifest=create(plan_dir=args.plan_dir,source_root=args.source_root,
                            end_session=args.end_session,
                            session_count=args.session_count,
                            symbols=_comma_list(args.symbols))
            report={
                "status":"IMMUTABLE_PLAN_CREATED_NO_API_CALL",
                "manifest":str(Path(args.plan_dir).expanduser().resolve()/"plan.json"),
                "session_count":len(manifest["sessions"]),
                "first_session":manifest["sessions"][0],
                "last_session":manifest["sessions"][-1],
                "batch_count":len(manifest["batches"]),
                "existing_verified":manifest["already_held_stock_day_snapshots"],
                "pending":manifest["pending_stock_day_snapshots"],
                "trading_allowed":False,
            }
        elif args.all_batches:
            preview=run_all(plan_dir=args.plan_dir)
            if not args.execute or not preview["remaining_before"]:
                report=run_all(plan_dir=args.plan_dir,execute=args.execute)
            else:
                key=os.getenv("KIS_APP_KEY","")
                secret=os.getenv("KIS_APP_SECRET","")
                if not key or not secret:
                    raise VenueProbeError("Missing local KIS credentials")
                token=_kis_token()
                report=run_all(plan_dir=args.plan_dir,execute=True,
                               key=key,secret=secret,token=token)
        else:
            if args.execute:
                # Never use KIS OAuth when preview-only or all saved sources
                # have already passed the integrity check.
                preview=run_batch(plan_dir=args.plan_dir,batch_index=args.batch)
                if preview["pending"]:
                    key=os.getenv("KIS_APP_KEY","")
                    secret=os.getenv("KIS_APP_SECRET","")
                    if not key or not secret:
                        raise VenueProbeError("Missing local KIS credentials")
                    token=_kis_token()
                    report=run_batch(plan_dir=args.plan_dir,batch_index=args.batch,
                                     execute=True,key=key,secret=secret,token=token)
                else:
                    report=run_batch(plan_dir=args.plan_dir,batch_index=args.batch,
                                     execute=True)
            else:
                report=run_batch(plan_dir=args.plan_dir,batch_index=args.batch)
        print(json.dumps(report,sort_keys=True,ensure_ascii=False,indent=2))
        return 0 if report["status"] not in (
            "STOPPED_ON_FAILURE_LIMIT","COMPLETED_WITH_FAILURES",
            "ALL_BATCHES_PARTIALLY_COLLECTED_HOLD") else 2
    except Exception as exc:
        # No vendor headers, tokens, raw provider msg1 or env values.
        err=type(exc).__name__
        parser.exit(2,"FAIL CLOSED: "+(
            err if err in {"VenueProbeError","DataError"} else "UnexpectedError"
        )+"\n")


if __name__=="__main__":
    raise SystemExit(main())

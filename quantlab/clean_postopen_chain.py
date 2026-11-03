"""Post-open evidence chain for the frozen 90-day clean paper cohort.

Separates TODAY'S strictly-timed six synthetic paper decisions from cumulative
historical gaps. Relates the WSL preopen readiness receipt to independently
audited original public quotes and immutable shadow account-day evidence.
NO broker endpoints, NO scheduler modifications, NO retroactive paper fills.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .clean_90d_evidence_coverage import build as coverage_build

SCHEMA="CLEAN_90D_POSTOPEN_CHAIN_V1_20261103"
KST=ZoneInfo("Asia/Seoul")
REQUIRED_PREOPEN_FROM=date(2026,11,4)
PREOPEN_SCHEMA="CLEAN_90D_PREOPEN_READINESS_V1_20261103"
CLOSED_KST_TIME=(10,50)
EXPECTED_PREOPEN_SLOTS={
    "08:46":((8,40),(9,0)),
    "09:07":((9,0),(9,10)),
    "09:12":((9,10),(9,16)),
}
SUCCESS_PREOPEN="READY_FOR_FUTURE_OBSERVATION_ONLY"
SUCCESS_IN_WINDOW="READY_FOR_FUTURE_OBSERVATION_ONLY"
MAX_PREOPEN_BYTES=32000


def _sha(payload:bytes)->str:
    return sha256(payload).hexdigest()


def _json_bytes(data:dict)->bytes:
    return (json.dumps(data,sort_keys=True,ensure_ascii=False,allow_nan=False,
                       indent=2)+"\n").encode()


def _load_preopen_receipts(*,directory:Path,day:date,asof:datetime)->dict:
    """Fail closed on a corrupted source pin; never infer Windows task runs."""
    counts={slot:[] for slot in EXPECTED_PREOPEN_SLOTS}
    evidence=[]
    late=[]
    if not directory.is_dir():
        return {"status":"NO_WSL_PREOPEN_RECEIPTS","slots":{k:False for k in counts},
                "source_sha256":{},"receipts_seen":0,
                "late_after_window_receipts":0,
                "windows_warm_task_run_proven":False}
    # Strict read-only evidence bounds: never parse arbitrary external paths.
    for file in sorted(directory.glob("readiness-*.json")):
        if file.is_symlink() or not file.is_file() or file.stat().st_size>MAX_PREOPEN_BYTES:
            raise ValueError("Unsafe preopen receipt")
        if (file.stat().st_mode&0o777)!=0o600:
            raise ValueError("Preopen source not private")
        raw=file.read_bytes()
        pinned=file.with_suffix(".json.sha256")
        if (not pinned.is_file() or pinned.is_symlink() or
            (pinned.stat().st_mode&0o777)!=0o600 or
            _sha(raw)!=pinned.read_text().strip()):
            raise ValueError("Preopen receipt SHA failure")
        record=json.loads(raw)
        if record.get("schema")!=PREOPEN_SCHEMA:
            raise ValueError("Unsupported preopen record schema")
        timestamp=datetime.fromisoformat(record["checked_at_utc"])
        if timestamp.tzinfo is None:
            raise ValueError("Naive preopen timestamp")
        clock=timestamp.astimezone(timezone.utc)
        if (clock>asof or clock.astimezone(KST).date()!=day):
            continue
        if (record.get("kst_date")!=str(day) or
            record.get("live_orders")!=0 or
            record.get("past_virtual_orders_created")!=0 or
            record.get("real_trading_enabled") is not False or
            record.get("strategy_promotion_enabled") is not False or
            record.get("frozen_accounts_verified")!=6 or
            record.get("future_09_24_observation_guaranteed") is not False):
            raise ValueError("Preopen receipt contradicts frozen research guards")
        evidence.append(file)
        clock_tuple=(clock.astimezone(KST).hour,clock.astimezone(KST).minute)
        if clock_tuple>=(10,20):
            late.append(file)
        for slot,(start,end) in EXPECTED_PREOPEN_SLOTS.items():
            if start<=clock_tuple<end:
                # A successful timer must explicitly assert connectivity and
                # registered/frozen observations; a generic systemd OK is not enough.
                good=(record.get("status")==SUCCESS_PREOPEN and
                      record.get("infrastructure_ready") is True and
                      record.get("calendar_timers_ready") is True and
                      record.get("public_market_reachable") is True and
                      record.get("observation_window_status") in (
                          "PREOPEN","OBSERVATION_WINDOW"))
                counts[slot].append(good)
    slot_good={slot:any(v) for slot,v in counts.items()}
    status=("PREOPEN_ALL_THREE_VERIFIED"
            if all(slot_good.values()) else
            "PREOPEN_PARTIAL_VERIFIED"
            if any(slot_good.values()) else "NO_VERIFIED_PREOPEN_WINDOW")
    return {"status":status,"slots":slot_good,
            "source_sha256":{p.name:_sha(p.read_bytes()) for p in evidence},
            "receipts_seen":len(evidence),
            "late_after_window_receipts":len(late),
            "windows_warm_task_run_proven":False}


def assess(*,db:Path|str,protocol:Path|str,preopen_dir:Path|str,
           now:datetime|None=None,rollout:date=REQUIRED_PREOPEN_FROM)->dict:
    current=now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Postopen evaluation requires aware clock")
    current=current.astimezone(timezone.utc)
    local=current.astimezone(KST)
    local_day=local.date()
    decision_day=local_day-timedelta(days=1)
    before_due=(local.hour,local.minute)<CLOSED_KST_TIME
    evidence=coverage_build(db=db,protocol=protocol,now=current)
    day_rows=[item for item in evidence["by_decision_utc_day"]
              if item["decision_utc_date"]==decision_day.isoformat()]
    preopen=_load_preopen_receipts(
        directory=Path(preopen_dir).expanduser().resolve(),
        day=local_day,asof=current)
    required=local_day>=rollout
    if not required:
        preopen_status="NOT_REQUIRED_BEFORE_PREOPEN_INSTALL_ROLLOUT"
    else:
        preopen_status=preopen["status"]
    if before_due:
        day_status="DUE_WINDOW_NOT_CLOSED_DO_NOT_IMPUTE"
        eligible_for_daily_completeness=False
    elif not day_rows:
        day_status="NO_ELIGIBLE_FROZEN_DECISION_DAY"
        eligible_for_daily_completeness=False
    elif (len(day_rows)==1 and day_rows[0]["state"]=="COMPLETE_VALID"
          and day_rows[0]["valid_count"]==6):
        day_status="SIX_OF_SIX_VALID_ORIGINAL_FORWARD_DECISIONS"
        eligible_for_daily_completeness=True
    elif len(day_rows)==1 and day_rows[0]["state"]=="ALL_MISSING":
        day_status="SIX_MISSING_NO_RETROACTIVE_REPLAY"
        eligible_for_daily_completeness=False
    elif len(day_rows)==1:
        day_status="PARTIAL_OR_INVALID_DAY_REQUIRES_INVESTIGATION"
        eligible_for_daily_completeness=False
    else:
        raise ValueError("Duplicate due decision dates")
    current_day=day_rows[0] if day_rows else None
    if day_status=="SIX_OF_SIX_VALID_ORIGINAL_FORWARD_DECISIONS":
        chain=("DAY_COMPLETE_WITH_PREOPEN_EVIDENCE"
               if required and preopen_status=="PREOPEN_ALL_THREE_VERIFIED" else
               "DAY_COMPLETE_PREOPEN_PARTIAL_OR_UNOBSERVED"
               if required else "DAY_COMPLETE_PREOPEN_NOT_YET_REQUIRED")
    elif before_due:
        chain="WINDOW_NOT_CLOSED_DO_NOT_ASSESS_FILLS"
    elif day_status=="NO_ELIGIBLE_FROZEN_DECISION_DAY":
        chain="OUTSIDE_FROZEN_EVALUATION_WINDOW"
    else:
        chain="TODAY_DECISION_INCOMPLETE_NO_BACKFILL"
    cumulative=evidence["status"]
    return {
        "schema":SCHEMA,"status":chain,
        "checked_at_utc":current.isoformat(),
        "kst_report_date":local_day.isoformat(),
        "due_utc_decision_date":decision_day.isoformat(),
        "rollout_preopen_enforced_from_kst_date":rollout.isoformat(),
        "preopen_requirements_enforced":required,
        "preopen_status":preopen_status,
        "preopen_evidence":preopen,
        "win32_login_resume_tasks_independently_attested_from_this_report":False,
        "day_utc_evidence":current_day,
        "today_decision_status":day_status,
        "today_all_six_valid":eligible_for_daily_completeness,
        "today_valid":current_day["valid_count"] if current_day else 0,
        "today_missing":current_day["missing_count"] if current_day else 0,
        "today_invalid":current_day["invalid_count"] if current_day else 0,
        "today_in_window_run_count":(
            current_day["run_evidence"]["in_window_count"] if current_day else 0),
        "today_late_run_count":(
            current_day["run_evidence"]["after_window_count"] if current_day else 0),
        "day_completion_approved_as_strategy_performance":False,
        "cumulative_cohort_status":cumulative,
        "cumulative_expected":evidence["expected_account_day_observations"],
        "cumulative_valid":evidence["valid_account_day_observations"],
        "cumulative_missing":evidence["missing_account_day_observations"],
        "cumulative_invalid":evidence["invalid_account_day_observations"],
        "original_protocol_sha256":evidence["protocol_sha256"],
        "source_stock_90d_account_event_snapshot_sha256":
            evidence["source_row_snapshot_sha256"],
        "historical_gaps_not_silenced":True,
        "new_paper_fills_or_historical_replay":0,
        "crypto_market_prices_written":False,
        "broker_orders":0,
        "strategy_returns":None,
        "strategy_sharpe":None,
        "model_training_or_live_trading_approved":False,
    }


def _write_receipt(report:dict,root:Path)->Path:
    dt=datetime.fromisoformat(report["checked_at_utc"])
    root=root.expanduser().resolve()/report["kst_report_date"]
    root.mkdir(parents=True,mode=0o700,exist_ok=True)
    root.chmod(0o700)
    name="postopen-"+dt.strftime("%Y%m%dT%H%M%S%fZ")+".json"
    dest=root/name
    payload=_json_bytes(report)
    fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    digest=dest.with_suffix(".json.sha256")
    fd=os.open(digest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as f:
        f.write(_sha(payload)+"\n")
    return dest


def verify(*,receipt:Path|str,db:Path|str,protocol:Path|str,
           preopen_dir:Path|str)->dict:
    path=Path(receipt).expanduser().resolve()
    raw=path.read_bytes()
    pin=path.with_suffix(".json.sha256")
    if (_sha(raw)!=pin.read_text().strip() or
        (path.stat().st_mode&0o777)!=0o600 or
        (pin.stat().st_mode&0o777)!=0o600 or
        (path.parent.stat().st_mode&0o777)!=0o700):
        raise ValueError("Postopen report SHA or permissions invalid")
    original=json.loads(raw)
    if original.get("schema")!=SCHEMA:
        raise ValueError("Unsupported postopen report schema")
    clock=datetime.fromisoformat(original["checked_at_utc"])
    rollout=date.fromisoformat(original["rollout_preopen_enforced_from_kst_date"])
    independent=assess(db=db,protocol=protocol,preopen_dir=preopen_dir,
                       now=clock,rollout=rollout)
    if independent!=original:
        raise ValueError("Postopen snapshot changed from independent due evidence")
    return original


def main(argv:list[str]|None=None)->int:
    a=argparse.ArgumentParser(description="No-order postopen daily chain verification")
    a.add_argument("--db",required=True)
    a.add_argument("--protocol",required=True)
    a.add_argument("--preopen-dir",required=True)
    a.add_argument("--report-dir",required=True)
    a.add_argument("--verify-receipt")
    args=a.parse_args(argv)
    try:
        if args.verify_receipt:
            result=verify(receipt=args.verify_receipt,db=args.db,
                          protocol=args.protocol,preopen_dir=args.preopen_dir)
            output=Path(args.verify_receipt).expanduser().resolve()
        else:
            result=assess(db=args.db,protocol=args.protocol,
                          preopen_dir=args.preopen_dir)
            output=_write_receipt(result,Path(args.report_dir))
            verify(receipt=output,db=args.db,protocol=args.protocol,
                   preopen_dir=args.preopen_dir)
        print(json.dumps({
            "status":result["status"],
            "today_valid":result["today_valid"],
            "today_missing":result["today_missing"],
            "historical_missing":result["cumulative_missing"],
            "preopen_status":result["preopen_status"],
            "receipt":str(output),
            "no_orders":True
        },sort_keys=True,ensure_ascii=False))
        return 0 if result["today_decision_status"] in (
            "SIX_OF_SIX_VALID_ORIGINAL_FORWARD_DECISIONS",
            "DUE_WINDOW_NOT_CLOSED_DO_NOT_IMPUTE",
            "NO_ELIGIBLE_FROZEN_DECISION_DAY") else 2
    except (OSError,ValueError,KeyError,TypeError,OverflowError):
        print(json.dumps({"status":"POSTOPEN_EVIDENCE_FAIL_CLOSED",
                          "no_orders":True,"backfilled":False}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

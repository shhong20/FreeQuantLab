"""Independent Windows Task Scheduler warmup + frozen clean paper due-day audit.

Windows TaskScheduler Operational is disabled on this host. A separate Windows
08:50 / 09:10 user task therefore records the EXISTING 08:40 / 09:05 task's
last-run exit code and exact timestamp in local, SHA-pinned sidecar receipts.
This code joins those receipts to independently reconstructed original clean
forward decision/quote evidence, never to broker orders or fake fills.
"""
from __future__ import annotations

import argparse
from datetime import date,datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path

from .clean_postopen_chain import assess as assess_postopen, KST

SCHEMA="CLEAN_WINDOWS_WARM_TO_POSTOPEN_V1_20261103"
WINDOWS_SCHEMA="FREEQUANTLAB_WINDOWS_WARM_RUN_RECEIPT_V1_20261103"
WINDOWS_TASK="FreeQuantLab-WSL-PreOpen-Warm"
WINDOWS_SLOTS={
    "08:40":((8,50),(8,55),(8,39),(8,51)),
    "09:05":((9,10),(9,15),(9,4),(9,11)),
}
MAX_RECEIPT_BYTES=8192


def _hash(raw:bytes)->str:
    return sha256(raw).hexdigest()


def _payload(obj:dict)->bytes:
    return (json.dumps(obj,ensure_ascii=False,sort_keys=True,indent=2,
                       allow_nan=False)+"\n").encode()


def _load_windows(*,folder:Path,date_kst:date,asof:datetime)->dict:
    slots={name:[] for name in WINDOWS_SLOTS}
    source={}
    if not folder.is_dir():
        return {"status":"WINDOWS_WARM_RECEIPTS_ABSENT",
                "slots":{k:False for k in slots},
                "source_sha256":{},"receipt_count":0,
                "windows_task_run_proven_for_both_slots":False,
                "cold_boot_or_host_awake_all_day_proven":False}
    # Files are created in the logged-in Windows user's LOCALAPPDATA
    # directory; this cannot verify Windows trust boundary cryptographically.
    for item in sorted(folder.glob("windowswarm-*.json")):
        if (item.is_symlink() or not item.is_file() or
            item.stat().st_size>MAX_RECEIPT_BYTES):
            raise ValueError("Unexpected Windows warm evidence payload")
        pin=item.with_suffix(".json.sha256")
        raw=item.read_bytes()
        if (not pin.is_file() or pin.is_symlink() or
            _hash(raw)!=pin.read_text(encoding="ascii").strip()):
            raise ValueError("Windows warm receipt SHA mismatch")
        record=json.loads(raw)
        if (record.get("schema")!=WINDOWS_SCHEMA or
            record.get("task_name")!=WINDOWS_TASK or
            record.get("mode")!="Capture" or
            record.get("broker_orders")!=0 or
            record.get("created_historical_paper_orders")!=0 or
            record.get("wrote_stock_or_crypto_prices") is not False or
            record.get("actual_cold_boot_or_sleep_resume_proven") is not False or
            record.get("actual_wsl_remained_online_after_execution_proven") is not False):
            raise ValueError("Windows warm evidence contradicts safety contract")
        captured=datetime.fromisoformat(record["audit_time_utc"])
        if captured.tzinfo is None:
            raise ValueError("Windows warm receipt timestamp timezone missing")
        captured=captured.astimezone(timezone.utc)
        if captured>asof or captured.astimezone(KST).date()!=date_kst:
            continue
        slot=record["scheduled_warm_slot_kst"]
        if slot not in slots:
            raise ValueError("Windows warm timestamp has no registered slot")
        observed=datetime.fromisoformat(record["warm_last_run_time_utc"]) if record.get(
            "warm_last_run_time_utc") else None
        if observed is not None:
            if observed.tzinfo is None:raise ValueError("Warm last-run timezone missing")
            observed=observed.astimezone(timezone.utc)
        local_now=captured.astimezone(KST)
        local_last=observed.astimezone(KST) if observed is not None else None
        audit_start,audit_end,last_start,last_end=WINDOWS_SLOTS[slot]
        audit_clock=(local_now.hour,local_now.minute)
        last_clock=(local_last.hour,local_last.minute) if local_last else None
        time_ok=(audit_start<=audit_clock<audit_end and
                 local_last is not None and local_last.date()==date_kst and
                 last_start<=last_clock<last_end and observed<=captured)
        claimed=record.get("successful_windows_warm_task_execution") is True
        expected=(record.get("existing_warm_config_match") is True and
                  record.get("evidence_window_valid") is True and
                  record.get("warm_executed_in_allowed_slot") is True and
                  record.get("warm_last_task_result")==0 and time_ok)
        if claimed!=expected:
            raise ValueError("Windows warm source claimed a false success")
        slots[slot].append(expected)
        source[item.name]=_hash(raw)
    finished={name:any(values) for name,values in slots.items()}
    status=("BOTH_WINDOWS_WARM_TASK_EXECUTIONS_ATTESTED"
            if all(finished.values()) else
            "SINGLE_WINDOWS_WARM_TASK_EXECUTION_ATTESTED"
            if any(finished.values()) else
            "WINDOWS_WARM_EXECUTION_NOT_ATTESTED")
    return {"status":status,"slots":finished,"source_sha256":source,
            "receipt_count":len(source),
            "windows_task_run_proven_for_both_slots":all(finished.values()),
            "cold_boot_or_host_awake_all_day_proven":False}


def assemble(*,db:Path|str,protocol:Path|str,
             preopen_dir:Path|str,windows_warm_dir:Path|str,
             now:datetime|None=None)->dict:
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Timezone-aware Windows trace as-of required")
    now=now.astimezone(timezone.utc)
    postopen=assess_postopen(db=db,protocol=protocol,preopen_dir=preopen_dir,
                             now=now)
    windows=_load_windows(folder=Path(windows_warm_dir).expanduser().resolve(),
                          date_kst=now.astimezone(KST).date(),asof=now)
    # Independently report actual readiness evidence even on days before the
    # scheduled rollout was mandatory. 'Not required' is a protocol policy,
    # not an assertion that three read-only readiness receipts did not exist.
    three_wsl_checks=postopen["preopen_evidence"]["status"]=="PREOPEN_ALL_THREE_VERIFIED"
    # A Windows Task Scheduler receipt shows that its action returned 0 at
    # its recorded time. A successful task does NOT prove WSL remained awake.
    return {
        "schema":SCHEMA,
        "status":("WINDOWS_WARM_AND_DAILY_OBSERVATION_EVIDENCE"
                  if windows["windows_task_run_proven_for_both_slots"] and
                     postopen["today_all_six_valid"] and
                     three_wsl_checks else
                  "DAILY_OBSERVATION_WITH_PARTIAL_BOOT_EVIDENCE"
                  if postopen["today_all_six_valid"] else
                  "TODAY_DUE_OBSERVATION_UNVERIFIED_NO_BACKFILL"),
        "checked_at_utc":now.isoformat(),
        "kst_date":postopen["kst_report_date"],
        "utc_decision_date":postopen["due_utc_decision_date"],
        "windows_warm_task_receipts":windows,
        "wsl_readiness_status":postopen["preopen_status"],
        "wsl_readiness_3_windows_verified":three_wsl_checks,
        "today_all_six_valid":postopen["today_all_six_valid"],
        "today_valid":postopen["today_valid"],
        "today_missing":postopen["today_missing"],
        "today_invalid":postopen["today_invalid"],
        "cumulative_missing":postopen["cumulative_missing"],
        "cumulative_cohort_status":postopen["cumulative_cohort_status"],
        "postopen_original_account_snapshot_sha256":postopen[
            "source_stock_90d_account_event_snapshot_sha256"],
        "frozen_protocol_sha256":postopen["original_protocol_sha256"],
        "windows_evidence_is_self_sha_not_cryptographic_host_attestation":True,
        "pc_powered_on_while_windows_off_proven":False,
        "new_fills_created":0,
        "broker_orders":0,
        "strategy_pnl":None,
        "trading_approved":False,
    }


def save(report:dict,root:Path)->Path:
    stamp=datetime.fromisoformat(report["checked_at_utc"])
    path=root.expanduser().resolve()/report["kst_date"]
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.chmod(0o700)
    target=path/("windowschain-"+stamp.strftime("%Y%m%dT%H%M%S%fZ")+".json")
    raw=_payload(report)
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"wb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    fd=os.open(target.with_suffix(".json.sha256"),
               os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as out:out.write(_hash(raw)+"\n")
    return target


def _canonicalize_empty_windows_source(report:dict)->dict:
    """Missing and empty receipt directories mean the same zero evidence.

    An immutable old report must survive creation of an empty directory.
    Any actual receipt, mismatched metadata, or positive attestation remains
    subject to the full original comparison.
    """
    windows=report.get("windows_warm_task_receipts")
    if not isinstance(windows,dict):
        return report
    if (windows.get("status") not in (
            "WINDOWS_WARM_RECEIPTS_ABSENT",
            "WINDOWS_WARM_EXECUTION_NOT_ATTESTED")
        or windows.get("receipt_count")!=0
        or windows.get("source_sha256")!={}
        or windows.get("slots")!={"08:40":False,"09:05":False}
        or windows.get("windows_task_run_proven_for_both_slots") is not False
        or windows.get("cold_boot_or_host_awake_all_day_proven") is not False):
        return report
    return {**report,"windows_warm_task_receipts":{
        **windows,"status":"WINDOWS_WARM_NO_QUALIFYING_RECEIPTS"}}


def verify(*,file:Path|str,db:Path|str,protocol:Path|str,
           preopen_dir:Path|str,windows_warm_dir:Path|str)->dict:
    path=Path(file).expanduser().resolve()
    data=path.read_bytes()
    pin=path.with_suffix(".json.sha256")
    if (_hash(data)!=pin.read_text().strip() or
        (path.stat().st_mode&0o777)!=0o600 or
        (pin.stat().st_mode&0o777)!=0o600 or
        (path.parent.stat().st_mode&0o777)!=0o700):
        raise ValueError("Windows chain report SHA or permissions mismatch")
    original=json.loads(data)
    if original.get("schema")!=SCHEMA:
        raise ValueError("Unexpected Windows evidence schema")
    clock=datetime.fromisoformat(original["checked_at_utc"])
    rebuilt=assemble(db=db,protocol=protocol,
                     preopen_dir=preopen_dir,windows_warm_dir=windows_warm_dir,
                     now=clock)
    if (_canonicalize_empty_windows_source(original)
            !=_canonicalize_empty_windows_source(rebuilt)):
        raise ValueError("Windows-to-WSL evidence chain not independently reproducible")
    return original


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description="Research-only Windows-to-WSL observed receipt audit")
    for arg in ("db","protocol","preopen-dir","windows-warm-dir","report-dir"):
        parser.add_argument("--"+arg,required=True)
    parser.add_argument("--verify-file")
    args=parser.parse_args(argv)
    kw={"db":args.db,"protocol":args.protocol,
        "preopen_dir":args.preopen_dir,"windows_warm_dir":args.windows_warm_dir}
    try:
        if args.verify_file:
            report=verify(file=args.verify_file,**kw)
            file=Path(args.verify_file)
        else:
            report=assemble(**kw)
            file=save(report,Path(args.report_dir))
            verify(file=file,**kw)
        print(json.dumps({
            "status":report["status"],
            "windows":report["windows_warm_task_receipts"]["status"],
            "wsl_preopen":report["wsl_readiness_status"],
            "daily_valid":report["today_valid"],
            "historical_missing":report["cumulative_missing"],
            "saved_report":str(file),"broker_orders":0
        },sort_keys=True))
        return 0 if report["today_all_six_valid"] else 2
    except (OSError,ValueError,TypeError,KeyError,OverflowError):
        print(json.dumps({"status":"WINDOWS_WSL_EVIDENCE_FAIL_CLOSED",
                          "broker_orders":0,"backfilled":False}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

"""Append-only acceptance of the *first actual due day* of a preregistered cohort.

Never writes to shadow ledger or fetches market data. The ordinary independent
clean Watchdog must PASS first. Then exactly six frozen account-day events
and two same-day read-only public quote rows are SHA256-sealed in a single
immutable, reproducible admission receipt. No PnL, fills, or broker claims.

Before 01:20 UTC on the first due morning, status is WAITING_NOT_DUE.
After that, missing, contaminated, or amended evidence must fail closed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date,datetime,time,timedelta,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys

from .clean_cohort import inspect_clean
from .monitor import _read_db
from .prospective import _utc,read_protocol

DEFAULT_DB="/home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db"
DEFAULT_PROTOCOL=(
    "/home/hong/apps/FreeQuantLab-clean-runtime/"
    "protocols/2026-10-21-btc-eth-clean-90d.json"
)
DEFAULT_RECEIPT_DIR="/home/hong/apps/FreeQuantLab/reports/clean-daily/first-day"

class AdmissionError(RuntimeError):
    pass

def _digest(rows:list[dict])->str:
    raw=json.dumps(rows,sort_keys=True,separators=(",",":"),
                   ensure_ascii=False,allow_nan=False).encode("utf-8")
    return sha256(raw).hexdigest()

def _first_day_snapshot(db:Path, day:str, manifest:dict)->tuple[list[dict],list[dict]]:
    with _read_db(db) as con:
        con.execute("BEGIN")
        try:
            if con.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                raise AdmissionError("Immutable clean cohort database failed integrity")
            events=[dict(r) for r in con.execute(
                "SELECT * FROM shadow_events WHERE decision_date=? ORDER BY track_id",
                (day,))]
            quotes=[dict(r) for r in con.execute(
                "SELECT * FROM shadow_quote_evidence WHERE decision_date=? "
                "ORDER BY symbol",(day,))]
            registered={r["track_id"] for r in con.execute(
                "SELECT track_id FROM shadow_accounts")}
        finally:
            con.execute("ROLLBACK")
    identities={a["track_id"] for a in manifest["account_set"]}
    if registered!=identities or len(identities)!=6:
        raise AdmissionError("Actual frozen account identities differ from preregistration")
    if len(events)!=6 or len(quotes)!=2:
        raise AdmissionError("First day requires six real virtual events and two quote rows")
    if {e["track_id"] for e in events}!=identities:
        raise AdmissionError("Every registered account must have exactly one due event")
    if {q["symbol"] for q in quotes}!={"KRW-BTC","KRW-ETH"}:
        raise AdmissionError("First day requires BTC and ETH evidence")
    session=(date.fromisoformat(day)+timedelta(days=1)).isoformat()
    if any(e["session_date"]!=session for e in events):
        raise AdmissionError("Virtual events were not recorded on next UTC session")
    if any(q["session_date"]!=session or
           q["source"]!="upbit_public_ticker" for q in quotes):
        raise AdmissionError("Public ticker evidence session/source mismatch")
    return events,quotes

def _write_once(target:Path,body:dict)->tuple[str,bool]:
    data=(json.dumps(body,sort_keys=True,ensure_ascii=False,
                     allow_nan=False,indent=2)+"\n").encode("utf-8")
    if target.parent.is_symlink() or target.is_symlink():
        raise AdmissionError("Symlink in first-day receipt destination")
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if target.exists():
        if not target.is_file() or target.read_bytes()!=data:
            raise AdmissionError("Previously sealed first-day receipt conflicts with actual evidence")
        return sha256(data).hexdigest(),False
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os,"O_NOFOLLOW"):
        flags|=os.O_NOFOLLOW
    try:
        fd=os.open(target,flags,0o600)
    except FileExistsError:
        if target.read_bytes()!=data:
            raise AdmissionError("Concurrent first-day receipt conflicts")
        return sha256(data).hexdigest(),False
    try:
        with os.fdopen(fd,"wb") as writer:
            writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
        parent_fd=os.open(target.parent,os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        # A partially-written one-time receipt is a forensic failure,
        # never automatically replaced or made into an accepted admission.
        raise
    return sha256(data).hexdigest(),True

def attest_first_day(db:str|Path=DEFAULT_DB,protocol:str|Path=DEFAULT_PROTOCOL,
                     receipt_dir:str|Path=DEFAULT_RECEIPT_DIR,*,
                     now:datetime|None=None)->dict:
    current=_utc(now)
    source=Path(db).resolve(strict=True)
    manifest_file=Path(protocol).resolve(strict=True)
    plan=read_protocol(manifest_file)
    first=date.fromisoformat(plan["first_eligible_decision_utc"])
    due_at=datetime.combine(first+timedelta(days=1),time(1,20),timezone.utc)
    base={"protocol_sha256":plan["sha256"],
          "first_eligible_decision_utc":first.isoformat(),
          "real_broker_orders":0,
          "automatic_strategy_promotion":False,
          "no_interim_returns_published":True}
    # This is a future-only day: do not materialize pseudo-acceptance before
    # the real paper observation window has completely elapsed.
    if current<due_at:
        return {**base,"status":"WAITING_FIRST_DUE_NOT_YET_MATURE",
                "first_day_due_after_utc":due_at.isoformat(),
                "receipt_created":False,"real_account_events_attested":0}
    with closing(_read_db(source)) as guard:
        version_before=guard.execute("PRAGMA data_version").fetchone()[0]
        report=inspect_clean(source,manifest_file,now=current)
        if guard.execute("PRAGMA data_version").fetchone()[0]!=version_before:
            raise AdmissionError("Clean ledger changed during independent audit")
    if (report["status"]!="PASS" or report["issues"]
        or report["recorded_due_account_days"]<6
        or report["invalid_due_account_days"]!=0
        or report["quote_proxy_status"]!="PASS"
        or report["ledger_audit_status"]!="PASS"):
        raise AdmissionError(
            "First completed due day did not pass independent clean Watchdog")
    with closing(_read_db(source)) as guard:
        version_check=guard.execute("PRAGMA data_version").fetchone()[0]
        events,quotes=_first_day_snapshot(source,first.isoformat(),plan)
        if guard.execute("PRAGMA data_version").fetchone()[0]!=version_check:
            raise AdmissionError("Clean ledger changed during evidence snapshot")
    doc={**base,"status":"FIRST_DAY_COMPLETE_VERIFIED_NO_PERFORMANCE_CLAIM",
         "first_day_account_day_events":6,
         "first_day_public_quote_evidence":2,
         "first_day_events_sha256":_digest(events),
         "first_day_public_quotes_sha256":_digest(quotes),
         "registered_account_set_sha256":_digest(
             [{"track_id":a["track_id"],"symbol":a["symbol"],
               "strategy":a["label"]} for a in
              sorted(plan["account_set"],key=lambda a:a["track_id"])]),
         "public_last_trade_is_not_an_executable_fill":True,
         "receipts_are_not_an_90d_strategy_approval":True,
         "read_only_shadow_evidence":True}
    dest=Path(receipt_dir).absolute()/("first-day-"+first.isoformat()+".json")
    with closing(_read_db(source)) as guard:
        last_version=guard.execute("PRAGMA data_version").fetchone()[0]
        latest_events,latest_quotes=_first_day_snapshot(source,first.isoformat(),plan)
        if (_digest(latest_events)!=doc["first_day_events_sha256"] or
            _digest(latest_quotes)!=doc["first_day_public_quotes_sha256"] or
            guard.execute("PRAGMA data_version").fetchone()[0]!=last_version):
            raise AdmissionError("Clean ledger evidence changed before receipt")
    digest,created=_write_once(dest,doc)
    return {**base,"status":doc["status"],"receipt_path":str(dest),
            "receipt_sha256":digest,"receipt_created":created,
            "first_day_account_day_events":6,
            "first_day_public_quote_evidence":2,
            "real_account_events_attested":6}

def _sync_seal_alerts(alert_db,receipt_dir,alerts):
    # Never overwrite ordinary independent Watchdog alert state.
    if not alert_db:
        return
    from .watchdog import persist_alert_transitions,deliver_pending,export_alert_history
    stamp=_utc(None)
    persist_alert_transitions(alert_db,alerts,now=stamp)
    deliver_pending(alert_db,toast=False,now=stamp)
    export_alert_history(alert_db,Path(receipt_dir)/"first-day-alert-history.json",now=stamp)

def main(argv=None)->int:
    p=argparse.ArgumentParser(description="Immutable first-day clean 90d evidence receipt, never trades")
    p.add_argument("--db",default=DEFAULT_DB)
    p.add_argument("--protocol",default=DEFAULT_PROTOCOL)
    p.add_argument("--receipt-dir",default=DEFAULT_RECEIPT_DIR)
    p.add_argument("--alert-db",default=None)
    args=p.parse_args(argv)
    try:
        result=attest_first_day(args.db,args.protocol,args.receipt_dir)
        if result["status"]=="FIRST_DAY_COMPLETE_VERIFIED_NO_PERFORMANCE_CLAIM":
            _sync_seal_alerts(args.alert_db,args.receipt_dir,[])
        print(json.dumps(result,sort_keys=True,ensure_ascii=False))
        return 0
    except (AdmissionError,ValueError,KeyError,FileNotFoundError,
            sqlite3.Error,TypeError,OSError) as exc:
        try:
            _sync_seal_alerts(args.alert_db,args.receipt_dir,[{
                "severity":"critical","code":"CLEAN_FIRST_DAY_SHA_ATTESTATION_BLOCKED",
                "message":"First-day SHA admission blocked; inspect independent ledger audit."
            }])
        except (OSError,RuntimeError,sqlite3.Error,ValueError) as alert_error:
            print("FIRST_DAY_ALERT_PERSISTENCE_FAILED: "+type(alert_error).__name__,file=sys.stderr)
        print(json.dumps({"status":"FIRST_DAY_ACCEPTANCE_BLOCKED",
                          "cause":type(exc).__name__,
                          "no_retroactive_virtual_events":True,
                          "real_orders":0}),file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())

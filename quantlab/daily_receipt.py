"""Read-only, evidence-linked daily acceptance of the first and later UTC paper days.

Executed *after* scheduled observation windows. It never runs the paper engines,
creates retroactive fills, changes investments, or reveals held-out returns.
"""
from __future__ import annotations

import argparse
from datetime import date,datetime,timedelta,timezone
import json
from pathlib import Path
import math
import os
import sys
from tempfile import NamedTemporaryFile

from .monitor import _read_db
from .ledger_audit import audit_shadow_ledger
from .quote_quality import execution_proxy_quality
from .prospective import _utc,read_protocol
from .protocol_compliance import _due_decision,assess_collection
from .qlib_lab import verify_plan
from .qlib_decision_audit import audit_decisions
from .market_archive import audit_archive

ASSETS=("KRW-BTC","KRW-ETH")
LANES=(
    ("original_90d","state/shadow.db",3),
    ("exaone","state/agent_lab_shadow.db",1),
    ("qlib","state/qlib_lab_shadow.db",1),
)
KST=timezone(timedelta(hours=9))


def _safe_report(path: Path,data: dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    encoded=(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+"\n").encode()
    # No destructive editing of virtual-account DBs; replace only daily reports.
    with NamedTemporaryFile(dir=path.parent,prefix=".receipt-",delete=False) as fh:
        temp=Path(fh.name)
        os.fchmod(fh.fileno(),0o600)
        fh.write(encoded)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def inspect_receipt(
    *, now:datetime|None=None,
    original_db: str|Path="state/shadow.db",
    agent_db:str|Path="state/agent_lab_shadow.db",
    qlib_db:str|Path="state/qlib_lab_shadow.db",
    protocol_file:str|Path="protocols/2026-10-20-btc-eth-90d.json",
    qlib_plan:str|Path="protocols/2026-10-20-qlib-lab.json",
    archive_db:str|Path="state/market_history.db",
)->dict:
    current=_utc(now)
    frozen=read_protocol(protocol_file)
    base_first=date.fromisoformat(frozen["first_eligible_decision_utc"])
    base_last=date.fromisoformat(frozen["last_eligible_decision_utc"])
    # Separately registered supplementary lane starts one day earlier.
    isolated_first=date(2026,10,20)
    isolated_last=date(2027,1,18)
    results=[]
    issues=[]
    due_count=0
    databases=(original_db,agent_db,qlib_db)
    for (name,_,strategies),db in zip(LANES,databases):
        first,last=(base_first,base_last) if name=="original_90d" else (isolated_first,isolated_last)
        due=_due_decision(current,first,last)
        due_day=due.isoformat() if due else None
        ledger=audit_shadow_ledger(db,now=current)
        quote=execution_proxy_quality(db)
        if ledger["status"]!="PASS":
            issues.append(name+":LEDGER_AUDIT_FAILED")
        if quote["status"] not in ("PASS","AWAITING_FIRST_OBSERVATION"):
            issues.append(name+":PUBLIC_QUOTE_EVIDENCE_INVALID")
        with _read_db(db) as con:
            con.execute("BEGIN")
            accounts=con.execute(
                "SELECT track_id,symbol,label FROM shadow_accounts"
            ).fetchall()
            records=con.execute(
                "SELECT e.track_id,a.symbol,e.decision_date,e.session_date,"
                "e.created_at,e.action,e.target,e.observed_price "
                "FROM shadow_events e JOIN shadow_accounts a ON a.track_id=e.track_id"
            ).fetchall()
            quotes=con.execute("SELECT * FROM shadow_quote_evidence").fetchall()
            con.execute("COMMIT")
        ids_by_asset={a:[x["track_id"] for x in accounts if x["symbol"]==a] for a in ASSETS}
        if len(accounts)!=2*strategies or any(len(v)!=strategies for v in ids_by_asset.values()):
            issues.append(name+":FROZEN_ACCOUNT_COUNT_OR_ASSET_UNEXPECTED")
        by_quote={(x["symbol"],x["decision_date"]):x for x in quotes}
        day_rows=[]
        if due_day:
            due_count+=len(accounts)
            for sym in ASSETS:
                observed=[r for r in records
                          if r["symbol"]==sym and r["decision_date"]==due_day]
                matching=by_quote.get((sym,due_day))
                unexpected=len(observed)!=strategies or {
                    r["track_id"] for r in observed
                }!=set(ids_by_asset[sym])
                invalid_time=False
                for row in observed:
                    stamp=datetime.fromisoformat(row["created_at"])
                    if stamp.tzinfo is None:
                        invalid_time=True
                        continue
                    stamp=stamp.astimezone(timezone.utc)
                    # The live public ticker is an opening-period proxy, not
                    # the next day's close, and must belong to decision+1.
                    if (stamp.date()!=due+timedelta(days=1)
                        or (stamp.hour,stamp.minute)<(0,10)
                        or (stamp.hour,stamp.minute)>=(1,20)):
                        invalid_time=True
                    if (row["session_date"]!=(due+timedelta(days=1)).isoformat()
                        or matching is None
                        or not math.isclose(
                            float(row["observed_price"]),
                            float(matching["public_ticker_price"]),
                            rel_tol=1e-10,abs_tol=1e-7)):
                        invalid_time=True
                if matching is not None and not observed:
                    invalid_time=True
                if unexpected:
                    issues.append(name+":"+sym+":EXPECTED_VIRTUAL_OBSERVATIONS_MISSING")
                if invalid_time:
                    issues.append(name+":"+sym+":INVALID_PUBLIC_QUOTE_OR_OBSERVATION_TIME")
                day_rows.append({
                    "symbol":sym,"utc_decision":due_day,
                    "expected_events":strategies,
                    "recorded_events":len(observed),
                    "unique_account_decisions":len({r["track_id"] for r in observed}),
                    "public_quote_saved":matching is not None,
                    "observation_time_valid":not invalid_time,
                })
        # The newest successful candle is not proof that every earlier
        # registered decision day was observed. A powered-off WSL host may
        # resume tomorrow; historical public candles must NEVER be turned
        # into invented paper fills. Check the whole due prefix read-only.
        history = {
            "expected_account_days": 0,
            "observed_account_days": 0,
            "missing_account_days": 0,
            "invalid_evidence_account_days": 0,
            "first_missing_utc_decision": None,
            "first_invalid_utc_decision": None,
        }
        if due is not None:
            by_event = {(r["track_id"], r["decision_date"]): r for r in records}
            for offset in range((due - first).days + 1):
                day = first + timedelta(days=offset)
                decision = day.isoformat()
                session = (day + timedelta(days=1)).isoformat()
                for symbol in ASSETS:
                    saved_quote = by_quote.get((symbol, decision))
                    for track_id in ids_by_asset[symbol]:
                        history["expected_account_days"] += 1
                        event = by_event.get((track_id, decision))
                        if event is None:
                            history["missing_account_days"] += 1
                            if history["first_missing_utc_decision"] is None:
                                history["first_missing_utc_decision"] = decision
                            continue
                        history["observed_account_days"] += 1
                        valid = event["session_date"] == session
                        try:
                            stamped = datetime.fromisoformat(event["created_at"])
                            valid = valid and stamped.tzinfo is not None
                            if stamped.tzinfo is not None:
                                stamped = stamped.astimezone(timezone.utc)
                                valid = valid and stamped.date().isoformat() == session
                                valid = valid and (0, 10) <= (stamped.hour, stamped.minute) < (1, 20)
                            valid = valid and saved_quote is not None
                            if saved_quote is not None:
                                valid = valid and math.isclose(
                                    float(event["observed_price"]),
                                    float(saved_quote["public_ticker_price"]),
                                    rel_tol=1e-10, abs_tol=1e-7,
                                )
                        except (TypeError, ValueError, OverflowError):
                            valid = False
                        if not valid:
                            history["invalid_evidence_account_days"] += 1
                            if history["first_invalid_utc_decision"] is None:
                                history["first_invalid_utc_decision"] = decision
            if history["missing_account_days"]:
                issues.append(name + ":HISTORICAL_VIRTUAL_OBSERVATIONS_MISSING")
            if history["invalid_evidence_account_days"]:
                issues.append(name + ":HISTORICAL_VIRTUAL_EVIDENCE_INVALID")
        results.append({
            "due_history":history,
            "lane":name,"status":"PASS" if due_day and not any(
                x.startswith(name+":") for x in issues
            ) else "WAITING_FIRST_DUE" if due_day is None else "REVIEW_REQUIRED",
            "first_eligible_utc_decision":first.isoformat(),
            "last_eligible_utc_decision":last.isoformat(),
            "latest_due_utc_decision":due_day,
            "registered_virtual_accounts":len(accounts),
            "total_virtual_event_rows":len(records),
            "quote_audit":quote["status"],
            "ledger_audit":ledger["status"],
            "due_date_evidence":day_rows,
        })
    qlib_evidence=audit_decisions(qlib_db,verify_plan(qlib_plan))
    if qlib_evidence["status"]=="FAIL":
        issues.append("qlib:FROZEN_MODEL_REPLAY_FAILED")
    compliance=assess_collection(original_db,protocol_file,now=current)
    prewindow=[x for x in compliance.get("issues",[])
               if x.get("code")=="PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY"]
    archive=audit_archive(archive_db,now=current)
    if archive["status"]!="PASS":
        issues.append("historical_market_archive:NOT_CURRENT")
    # Keep research-integrity contamination as a separate alert, rather than
    # confusing it with operational daily data collection completeness.
    research_issues=[{
        "code":x["code"],
        "events":x["prewindow_events"],
        "virtual_orders":x["prewindow_virtual_orders"],
        "first_day":x["first_unexpected_decision_utc"],
    } for x in prewindow]
    operational="INCOMPLETE" if issues else "WAITING_FIRST_DUE" if due_count==0 else "PASS"
    return {
        "status":operational,
        "captured_at_utc":current.isoformat(),
        "kst_receipt_date":current.astimezone(KST).date().isoformat(),
        "lanes":results,
        "issues":sorted(set(issues)),
        "research_integrity_status":"PREWINDOW_REVIEW_REQUIRED" if research_issues else "PASS",
        "research_integrity_issues":research_issues,
        "qlib_replay_status":qlib_evidence["status"],
        "qlib_replayed_model_decisions":qlib_evidence["fully_replayed_decisions"],
        "historical_market_archive":archive["status"],
        "due_virtual_account_days":due_count,
        "original_90d_returns":None,
        "market_data_backfill_is_not_a_paper_fill":True,
        "virtual_only":True,
        "real_orders_enabled":False,
        "manual_model_promotion_only":True,
    }


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="No-write proof of each due virtual decision day")
    parser.add_argument("--report-dir",default="reports/daily-receipts")
    parser.add_argument("--original-db",default="state/shadow.db")
    parser.add_argument("--agent-db",default="state/agent_lab_shadow.db")
    parser.add_argument("--qlib-db",default="state/qlib_lab_shadow.db")
    parser.add_argument("--protocol",default="protocols/2026-10-20-btc-eth-90d.json")
    parser.add_argument("--qlib-plan",default="protocols/2026-10-20-qlib-lab.json")
    parser.add_argument("--archive-db",default="state/market_history.db")
    args=parser.parse_args(argv)
    try:
        r=inspect_receipt(original_db=args.original_db,agent_db=args.agent_db,
                          qlib_db=args.qlib_db,protocol_file=args.protocol,
                          qlib_plan=args.qlib_plan,archive_db=args.archive_db)
        path=Path(args.report_dir)/(r["kst_receipt_date"]+".json")
        _safe_report(path,r)
        print(json.dumps({
            "status":r["status"],"report":str(path),
            "research_integrity_status":r["research_integrity_status"],
            "due_virtual_account_days":r["due_virtual_account_days"],
            "qlib_replay_status":r["qlib_replay_status"],
            "original_90d_returns":None,
        },ensure_ascii=False))
        return 2 if r["status"]=="INCOMPLETE" else 0
    except (OSError,ValueError,KeyError,RuntimeError) as exc:
        print("DAILY RECEIPT FAILED CLOSED: "+type(exc).__name__+": "+str(exc)[:160],file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

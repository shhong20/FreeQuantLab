"""Independent forward-cohort observation coverage and signal-evidence audit.

READ-ONLY: snapshots immutable protocol + clean paper ledger. Missing observed
days remain missing; late service starts never become past virtual fills.
This is not a completed performance study and cannot promote a strategy.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

from .clean_cohort import inspect_clean
from .monitor import _read_db
from .prospective import _utc, read_protocol
from .protocol_compliance import assess_collection, verify_frozen_accounts
from .ledger_audit import audit_shadow_ledger
from .quote_quality import execution_proxy_quality

VERSION="CLEAN_90D_ACCOUNT_DAY_COVERAGE_V1_20261103"
STATUS_MISSING="INCOMPLETE_COHORT_COVERAGE_NO_STRATEGY_PERFORMANCE"
STATUS_COMPLETE="OBSERVATION_COVERAGE_ONLY_NOT_PERFORMANCE_APPROVED"
ACTIONS=("BUY","SELL","HOLD")
SOURCE_COLUMNS={
    "accounts":("track_id","provider","symbol","currency","label","spec_json",
                "settings_json","registered_utc","frozen_bar"),
    "events":("track_id","decision_date","session_date","action","target",
              "observed_price","quantity","simulated_cost","created_at"),
    "quotes":("symbol","decision_date","session_date","public_ticker_price",
              "observed_at_utc","source"),
    "runs":("run_id","created_at","status","details_json"),
}


def _jsonbytes(obj)->bytes:
    return (json.dumps(obj,sort_keys=True,ensure_ascii=False,
                       indent=2,allow_nan=False)+"\n").encode()


def _digest(obj)->str:
    return sha256(_jsonbytes(obj)).hexdigest()


def _select(db:Path,now:datetime)->dict:
    """Read consistent, due-window-limited records, including late run receipts."""
    stamp=now.isoformat()
    with _read_db(db) as con:
        con.execute("BEGIN")
        tables={
            "accounts":con.execute(
                "SELECT "+",".join(SOURCE_COLUMNS["accounts"])+
                " FROM shadow_accounts ORDER BY track_id").fetchall(),
            "events":con.execute(
                "SELECT "+",".join(SOURCE_COLUMNS["events"])+
                " FROM shadow_events WHERE created_at<=? "
                "ORDER BY track_id,decision_date",(stamp,)).fetchall(),
            "quotes":con.execute(
                "SELECT "+",".join(SOURCE_COLUMNS["quotes"])+
                " FROM shadow_quote_evidence WHERE observed_at_utc<=? "
                "ORDER BY symbol,decision_date",(stamp,)).fetchall(),
            "runs":con.execute(
                "SELECT "+",".join(SOURCE_COLUMNS["runs"])+
                " FROM shadow_runs WHERE created_at<=? ORDER BY run_id",
                (stamp,)).fetchall(),
        }
        con.execute("COMMIT")
    return {name:[dict(row) for row in rows] for name,rows in tables.items()}


def _validated(event:dict,quote:dict|None,now:datetime)->bool:
    try:
        when=datetime.fromisoformat(event["created_at"])
        session=date.fromisoformat(event["session_date"])
        decision=date.fromisoformat(event["decision_date"])
        if (when.tzinfo is None or when.astimezone(timezone.utc)>now or
            (session-decision).days!=1 or
            when.astimezone(timezone.utc).date()!=session or
            not (10<=when.astimezone(timezone.utc).hour*60+
                  when.astimezone(timezone.utc).minute<80) or
            event["action"] not in ACTIONS or
            type(event["target"]) is not int or event["target"] not in (0,1) or
            not quote or quote["source"]!="upbit_public_ticker" or
            quote["session_date"]!=event["session_date"] or
            quote["decision_date"]!=event["decision_date"]):
            return False
        qt=datetime.fromisoformat(quote["observed_at_utc"])
        if qt.tzinfo is None or abs((when-qt).total_seconds())>120:
            return False
        price=float(event["observed_price"])
        qprice=float(quote["public_ticker_price"])
        if (not math.isfinite(price) or price<=0 or
            not math.isfinite(qprice) or qprice<=0 or
            not math.isclose(price,qprice,rel_tol=1e-10,abs_tol=1e-7)):
            return False
        if (event["action"]=="BUY" and event["target"]!=1 or
            event["action"]=="SELL" and event["target"]!=0):
            return False
        return True
    except (ValueError,TypeError,KeyError,OverflowError):
        return False


def _run_metadata(runs:list[dict],session_date:str)->dict:
    receipts=[]
    for r in runs:
        try:
            dt=datetime.fromisoformat(r["created_at"]).astimezone(timezone.utc)
            if dt.date().isoformat()!=session_date:
                continue
            details=json.loads(r["details_json"])
            if (not isinstance(details,dict) or
                details.get("real_orders_enabled") is not False or
                details.get("promotion_enabled") is not False):
                raise ValueError("Unexpected clean run permissions")
            statuses=[str(v.get("status","UNRECOGNIZED")) for v in details.get("results",[])
                      if isinstance(v,dict)]
            minute=dt.hour*60+dt.minute
            receipts.append({
                "utc_timestamp":dt.isoformat(),
                "top_level_status":r["status"],
                "result_statuses":sorted(set(statuses)),
                "in_observation_window":10<=minute<80,
                "after_observation_window":minute>=80,
            })
        except (KeyError,TypeError,ValueError,AttributeError) as exc:
            raise ValueError("Invalid stored clean run receipt") from exc
    return {
        "count":len(receipts),
        "in_window_count":sum(x["in_observation_window"] for x in receipts),
        "after_window_count":sum(x["after_observation_window"] for x in receipts),
        "includes_outside_observation_window_code":any(
            "OUTSIDE_OBSERVATION_WINDOW" in x["result_statuses"] for x in receipts),
        "receipts":receipts,
    }


def build(*,db:Path|str,protocol:Path|str,now:datetime|None=None,
          strict_current_audit:bool=True)->dict:
    current=_utc(now)
    db=Path(db).resolve()
    protocol=Path(protocol).resolve()
    plan=read_protocol(protocol)
    first=date.fromisoformat(plan["first_eligible_decision_utc"])
    last=date.fromisoformat(plan["last_eligible_decision_utc"])
    due=assess_collection(db,protocol,now=current)
    if due.get("status") in ("INVALID_LEDGER","INVALID_PROTOCOL_IDENTITY"):
        raise ValueError("Invalid ledger or frozen account identity")
    if due.get("protocol_sha256")!=plan["sha256"]:
        raise ValueError("Frozen manifest changed")
    if (audit_shadow_ledger(db,now=current)["status"]!="PASS" or
        execution_proxy_quality(db)["status"] not in (
            "PASS","AWAITING_FIRST_OBSERVATION")):
        raise ValueError("Independent ledger/quote audit did not pass")
    source=_select(db,current)
    frozen=verify_frozen_accounts(source["accounts"],protocol,now=current)
    if frozen["status"]!="PASS":
        raise ValueError("Frozen accounts do not match protocol")
    expected={p["track_id"]:p for p in plan["account_set"]}
    if set(a["track_id"] for a in source["accounts"])!=set(expected):
        raise ValueError("Unexpected clean account set")
    latest_due=due.get("latest_due_decision_utc")
    days=[] if latest_due is None else [
        (first+timedelta(days=i)).isoformat()
        for i in range((date.fromisoformat(latest_due)-first).days+1)]
    events={(e["track_id"],e["decision_date"]):e for e in source["events"]}
    if len(events)!=len(source["events"]):
        raise ValueError("Duplicate shadow event keys")
    quotes={(q["symbol"],q["decision_date"]):q for q in source["quotes"]}
    if len(quotes)!=len(source["quotes"]):
        raise ValueError("Duplicate quote evidence keys")
    actions_by_track={tid:Counter() for tid in expected}
    rows=[]
    for day in days:
        session=(date.fromisoformat(day)+timedelta(days=1)).isoformat()
        meta=_run_metadata(source["runs"],session)
        cell=[]
        for declared in plan["account_set"]:
            tid=declared["track_id"]
            event=events.get((tid,day))
            quote=quotes.get((declared["symbol"],day))
            if event is None:
                state="MISSING"
                action=None
                target=None
            else:
                state="VALID" if _validated(event,quote,current) else "INVALID"
                action=event["action"]
                target=event["target"]
                if state=="VALID":
                    actions_by_track[tid][action]+=1
            cell.append({
                "symbol":declared["symbol"],"strategy":declared["label"],
                "observation_state":state,
                "recorded_action":action,
                "target":target,
                "public_quote_source_verified":state=="VALID",
            })
        tally=Counter(x["observation_state"] for x in cell)
        if tally["MISSING"]==len(expected):
            if meta["includes_outside_observation_window_code"] and meta["after_window_count"]:
                gap_reason="AFTER_WINDOW_RUN_RECEIPT_WITH_NO_DECISION"
            elif not meta["count"]:
                gap_reason="NO_RUN_RECEIPT_FOR_EXPECTED_SESSION"
            else:
                gap_reason="RUN_RECEIPT_BUT_NO_RECORDED_DECISION"
        elif tally["VALID"]==len(expected):
            gap_reason=None
        else:
            gap_reason="PARTIAL_OR_INVALID_EVIDENCE_REQUIRES_INVESTIGATION"
        rows.append({
            "decision_utc_date":day,
            "expected_execution_utc_date":session,
            "state":("COMPLETE_VALID" if tally["VALID"]==len(expected)
                     else "ALL_MISSING" if tally["MISSING"]==len(expected)
                     else "PARTIAL_OR_INVALID"),
            "valid_count":tally["VALID"],
            "missing_count":tally["MISSING"],
            "invalid_count":tally["INVALID"],
            "run_evidence":meta,
            "gap_receipt_classification":gap_reason,
            "accounts":cell,
        })
    account_stats=[]
    for a in sorted(plan["account_set"],key=lambda p:(p["symbol"],p["label"])):
        matches=[c for r in rows for c in r["accounts"]
                 if c["symbol"]==a["symbol"] and c["strategy"]==a["label"]]
        tally=Counter(v["observation_state"] for v in matches)
        freq=actions_by_track[a["track_id"]]
        account_stats.append({
            "symbol":a["symbol"],"strategy":a["label"],
            "due_decision_days":len(days),
            "valid_observed_days":tally["VALID"],
            "missing_days":tally["MISSING"],
            "invalid_days":tally["INVALID"],
            "action_counts_on_valid_days":{k:freq[k] for k in ACTIONS},
            "action_evidence_validity_not_prediction_accuracy":True,
            "independent_strategy_return":None,
            "strategy_quality_not_yet_certifiable":True,
        })
    total=len(expected)*len(days)
    valid=sum(x["valid_count"] for x in rows)
    missing=sum(x["missing_count"] for x in rows)
    invalid=sum(x["invalid_count"] for x in rows)
    if (valid+missing+invalid!=total or
        total!=due["expected_due_days_per_account"]*len(expected)):
        raise ValueError("Account-day tally does not match protocol collection")
    if strict_current_audit:
        current_audit=inspect_clean(db,protocol,now=current)
        if (current_audit["expected_due_account_days"]!=total or
            current_audit["recorded_due_account_days"]!=valid+invalid or
            current_audit["invalid_due_account_days"]!=invalid or
            current_audit["protocol_sha256"]!=plan["sha256"]):
            raise ValueError("Independent clean protocol audit disagrees")
        audit_status=current_audit["status"]
        ledger_status=current_audit["ledger_audit_status"]
        quote_status=current_audit["quote_proxy_status"]
    else:
        audit_status="NOT_REEXECUTED_FIXED_AS_OF_SOURCE_RECHECKED"
        ledger_status="PASS"
        quote_status="PASS"
    complete=sum(x["state"]=="COMPLETE_VALID" for x in rows)
    missed=sum(x["state"]=="ALL_MISSING" for x in rows)
    return {
        "schema_version":VERSION,
        "status":STATUS_MISSING if missing or invalid else STATUS_COMPLETE,
        "checked_at_utc":current.isoformat(),
        "protocol_sha256":plan["sha256"],
        "frozen_first_decision_date":first.isoformat(),
        "frozen_last_decision_date":last.isoformat(),
        "evaluation_not_before_utc":plan["evaluation_not_before_utc"],
        "due_decision_days":len(days),
        "frozen_account_count":len(expected),
        "expected_account_day_observations":total,
        "valid_account_day_observations":valid,
        "missing_account_day_observations":missing,
        "invalid_account_day_observations":invalid,
        "complete_decision_days":complete,
        "all_missing_decision_days":missed,
        "first_missing_decision_date":next((r["decision_utc_date"] for r in rows
                                            if r["missing_count"]),None),
        "days_with_late_window_run_and_no_decision":sum(
            r["gap_receipt_classification"]=="AFTER_WINDOW_RUN_RECEIPT_WITH_NO_DECISION"
            for r in rows),
        "run_receipt_is_not_a_completed_virtual_observation":True,
        "missing_reason_is_receipt_evidence_not_confirmed_power_outage":True,
        "independent_ledger_audit":ledger_status,
        "independent_quote_audit":quote_status,
        "independent_clean_status_at_capture":audit_status,
        "source_row_snapshot_sha256":_digest(source),
        "observed_action_counts_are_not_prediction_skill":True,
        "no_backdated_paper_fills":True,
        "no_missing_days_imputed":True,
        "strategy_returns":None,
        "strategy_sharpe":None,
        "research_eligible_for_promotion":False,
        "paper_or_live_trading_approved":False,
        "broker_orders":0,
        "other_shadow_research_tracks_not_mixed_into_clean_cohort":True,
        "strategy_observation_quality":account_stats,
        "by_decision_utc_day":rows,
    }


def verify(*,output_dir:Path|str,db:Path|str,protocol:Path|str)->dict:
    directory=Path(output_dir).expanduser().resolve()
    body=(directory/"report.json").read_bytes()
    if sha256(body).hexdigest()!=(directory/"report.sha256").read_text().strip():
        raise ValueError("Coverage report hash mismatch")
    original=json.loads(body)
    clock=datetime.fromisoformat(original["checked_at_utc"])
    if clock.tzinfo is None:
        raise ValueError("Coverage audit time must be timezone aware")
    # Reconstruct the originally due matrix only; later rows cannot fabricate
    # an earlier completed decision. The source subset is bounded by clock.
    independent=build(db=db,protocol=protocol,now=clock,
                      strict_current_audit=False)
    fields=("independent_clean_status_at_capture","independent_ledger_audit",
            "independent_quote_audit")
    for f in fields:
        independent[f]=original.get(f)
    if independent!=original:
        raise ValueError("Coverage report disagrees with frozen source evidence")
    if (directory.stat().st_mode&0o777!=0o700 or
        any((directory/name).stat().st_mode&0o777!=0o600
            for name in ("report.json","report.sha256"))):
        raise ValueError("Coverage report permissions unsafe")
    return original


def capture(*,db:Path|str,protocol:Path|str,
            output_dir:Path|str,now:datetime|None=None)->dict:
    target=Path(output_dir).expanduser().resolve()
    if target.exists():
        raise ValueError("Existing immutable coverage report; never overwrite")
    report=build(db=db,protocol=protocol,now=now)
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".clean-coverage-",dir=target.parent))
    try:
        stage.chmod(0o700)
        body=_jsonbytes(report)
        for name,data in (("report.json",body),
                          ("report.sha256",(sha256(body).hexdigest()+"\n").encode())):
            path=stage/name
            path.write_bytes(data)
            path.chmod(0o600)
        if target.exists():
            raise ValueError("Concurrent coverage output exists")
        stage.rename(target)
    finally:
        if stage.exists():shutil.rmtree(stage)
    verify(output_dir=target,db=db,protocol=protocol)
    return report


def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser(description="90-day clean shadow coverage; no returns")
    ap.add_argument("--db",required=True)
    ap.add_argument("--protocol",required=True)
    ap.add_argument("--output-dir",required=True)
    ap.add_argument("--verify-only",action="store_true")
    args=ap.parse_args(argv)
    try:
        kw=dict(db=args.db,protocol=args.protocol,output_dir=args.output_dir)
        result=verify(**kw) if args.verify_only else capture(**kw)
        print(json.dumps({
            "status":result["status"],
            "due_account_days":result["expected_account_day_observations"],
            "valid":result["valid_account_day_observations"],
            "missing":result["missing_account_day_observations"],
            "invalid":result["invalid_account_day_observations"],
            "late_run_no_decision_days":result["days_with_late_window_run_and_no_decision"],
            "returns":None,"broker_orders":0,"approved":False
        },ensure_ascii=False,sort_keys=True))
        return 0
    except (KeyError,ValueError,TypeError,OSError):
        print(json.dumps({"status":"CLEAN_COVERAGE_REPORT_FAIL_CLOSED",
                          "broker_orders":0,"approved":False}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

"""Immutable, Git-versionable prospective Shadow Paper research protocol.

Pre-registers FUTURE observation dates and existing frozen strategy identities.
No API calls to brokers, no order permissions and no strategy promotion.
The current public dashboard shows interim PnL; so this is preregistration,
NOT an independently blinded test. Manual selection still contaminates inference.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

from .ledger_audit import audit_shadow_ledger
from .monitor import _read_db


class ProtocolError(ValueError):
    pass


def _canonical(data: dict) -> bytes:
    return json.dumps(data,sort_keys=True,separators=(",",":"),
                      ensure_ascii=True,allow_nan=False).encode("utf-8")


def _utc(now: datetime | None = None) -> datetime:
    stamp=now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ProtocolError("Pre-registration time must be timezone-aware")
    return stamp.astimezone(timezone.utc)


def _hash_track(a) -> str:
    return hashlib.sha256(_canonical({
        "track_id":a["track_id"],"provider":a["provider"],
        "symbol":a["symbol"],"label":a["label"],
        "spec":json.loads(a["spec_json"]),
        "settings":json.loads(a["settings_json"]),
        "registered_utc":a["registered_utc"],
        "frozen_bar":a["frozen_bar"],
    })).hexdigest()


def lock_protocol(
    shadow_db: str | Path, target: str | Path, *,
    now: datetime | None = None, days: int = 90,
) -> dict:
    """One-shot O_EXCL write; impossible to move the start back through CLI."""
    instant=_utc(now)
    if not 30 <= days <= 365:
        raise ProtocolError("Future evaluation horizon must be 30..365 days")
    ledger=audit_shadow_ledger(shadow_db,now=instant)
    if ledger["status"]!="PASS":
        raise ProtocolError("Frozen ledger must pass full read-only audit")
    with _read_db(shadow_db) as con:
        accounts=con.execute("SELECT * FROM shadow_accounts ORDER BY symbol,label").fetchall()
    if not accounts:
        raise ProtocolError("No registered accounts")
    seen=set()
    tracks=[]
    for a in accounts:
        if a["track_id"] in seen:
            raise ProtocolError("Repeated track id")
        seen.add(a["track_id"])
        reg=datetime.fromisoformat(a["registered_utc"])
        if reg.tzinfo is None or reg.astimezone(timezone.utc)>instant:
            raise ProtocolError("Registration timestamp is after protocol lock")
        tracks.append({
            "track_id":a["track_id"],"symbol":a["symbol"],"label":a["label"],
            "spec_and_account_sha256":_hash_track(a),
        })
    first=instant.date()+timedelta(days=1)
    last=first+timedelta(days=days-1)
    frozen={
        "schema_version":1,
        "protocol_kind":"preregistered_forward_virtual_account_observation",
        "created_at_utc":instant.isoformat(),
        "first_eligible_decision_utc":first.isoformat(),
        "last_eligible_decision_utc":last.isoformat(),
        "evaluation_not_before_utc":(last+timedelta(days=1)).isoformat()+"T00:00:00+00:00",
        "required_complete_utc_days":days,
        "benchmark_strategy_label":"buy_hold_20pct",
        "account_set":tracks,
        "predeclared_questions":[
            "Is candidate log return greater than same-symbol 20-percent-exposure buy/hold?",
            "Are all prespecified UTC decision days observed, and was the account never halted?",
        ],
        "real_broker_order_permission":False,
        "automatic_strategy_promotion":False,
        "blinded_holdout":False,
        "limitations":"Interim equity is visible on the dashboard. Not a blinded or independent-outcome experiment.",
    }
    frozen["sha256"]=hashlib.sha256(_canonical(frozen)).hexdigest()
    dest=Path(target)
    dest.parent.mkdir(parents=True,exist_ok=True)
    try:
        with dest.open("xb") as handle:
            os.chmod(dest,0o600)
            handle.write(json.dumps(frozen,indent=2,ensure_ascii=False).encode("utf-8")+b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ProtocolError("Pre-registration already locked. Never overwrite or backdate.") from exc
    return frozen


def read_protocol(source: str | Path) -> dict:
    path=Path(source)
    if not path.is_file():
        raise ProtocolError("Protocol manifest does not exist")
    body=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body,dict) or body.get("schema_version")!=1 or not isinstance(body.get("sha256"),str):
        raise ProtocolError("Invalid protocol schema")
    declared=body["sha256"]
    to_hash={key:value for key,value in body.items() if key!="sha256"}
    if hashlib.sha256(_canonical(to_hash)).hexdigest()!=declared:
        raise ProtocolError("Protocol content hash changed")
    if not isinstance(body.get("account_set"),list) or not body["account_set"]:
        raise ProtocolError("Protocol account set missing")
    if len({t["track_id"] for t in body["account_set"]})!=len(body["account_set"]):
        raise ProtocolError("Repeated frozen track id in protocol")
    start=date.fromisoformat(body["first_eligible_decision_utc"])
    end=date.fromisoformat(body["last_eligible_decision_utc"])
    if (end-start).days+1!=body["required_complete_utc_days"]:
        raise ProtocolError("Protocol date horizon mismatch")
    if datetime.fromisoformat(body["evaluation_not_before_utc"])!=datetime.combine(
        end+timedelta(days=1),datetime.min.time(),timezone.utc
    ):
        raise ProtocolError("Protocol evaluation opening mismatch")
    if (body.get("real_broker_order_permission") is not False or
        body.get("automatic_strategy_promotion") is not False or
        body.get("blinded_holdout") is not False):
        raise ProtocolError("Unsupported permission or blind status")
    return body


def inspect_protocol(
    shadow_db: str | Path, manifest: str | Path, *,
    now: datetime | None = None,
) -> dict:
    """No outcome exposure until cutoff; does not and cannot place any orders."""
    current=_utc(now)
    frozen=read_protocol(manifest)
    first=date.fromisoformat(frozen["first_eligible_decision_utc"])
    last=date.fromisoformat(frozen["last_eligible_decision_utc"])
    opening=datetime.fromisoformat(frozen["evaluation_not_before_utc"])
    ledger=audit_shadow_ledger(shadow_db,now=current)
    if ledger["status"]!="PASS":
        return {"status":"INVALID_LEDGER","protocol_sha256":frozen["sha256"],
                "issues":ledger["issues"],"real_orders_enabled":False,
                "eligible_for_promotion":False}
    with _read_db(shadow_db) as con:
        accounts=con.execute("SELECT * FROM shadow_accounts ORDER BY symbol,label").fetchall()
        observed=con.execute(
            "SELECT track_id,decision_date,equity,action FROM shadow_events "
            "WHERE decision_date >= ? AND decision_date <= ? "
            "ORDER BY track_id,decision_date",
            (first.isoformat(),last.isoformat()),
        ).fetchall()
        prior=con.execute(
            "SELECT track_id,decision_date,equity,action FROM shadow_events "
            "WHERE decision_date < ? ORDER BY track_id,decision_date",
            (first.isoformat(),),
        ).fetchall()
    live={a["track_id"]:a for a in accounts}
    if set(live)!=set(x["track_id"] for x in frozen["account_set"]):
        raise ProtocolError("Account universe changed since locking")
    for expected in frozen["account_set"]:
        if _hash_track(live[expected["track_id"]])!=expected["spec_and_account_sha256"]:
            raise ProtocolError("Account strategy/config changed since locking")
    prewindow_events=len(prior)
    prewindow_orders=sum(row["action"] in ("BUY","SELL") for row in prior)
    if current<opening:
        return {
            "status":"LOCKED_PRE_WINDOW_ACTIVITY_REVIEW" if prior else "LOCKED_UNTIL_FUTURE_CUTOFF",
            "protocol_sha256":frozen["sha256"],
            "first_eligible_decision_utc":first.isoformat(),
            "last_eligible_decision_utc":last.isoformat(),
            "evaluation_not_before_utc":opening.isoformat(),
            "days_until_evaluation":max(1,(opening.date()-current.date()).days),
            "observed_strategy_returns":None,
            "prewindow_virtual_events":prewindow_events,
            "prewindow_virtual_orders":prewindow_orders,
            "opening_inventory_requires_manual_review":bool(prewindow_orders),
            "real_orders_enabled":False,"eligible_for_promotion":False,
        }
    by_track={x["track_id"]:[] for x in frozen["account_set"]}
    for e in observed:
        if e["track_id"] in by_track:
            by_track[e["track_id"]].append(e)
    before={t:[] for t in by_track}
    for e in prior:
        if e["track_id"] in before:
            before[e["track_id"]].append(e)
    required=[first+timedelta(days=i) for i in range(frozen["required_complete_utc_days"])]
    evaluations=[]
    by_symbol_hold={}
    for t in frozen["account_set"]:
        key=t["track_id"]
        obs=by_track[key]
        got=[date.fromisoformat(row["decision_date"]) for row in obs]
        complete=got==required
        initial=float(before[key][-1]["equity"]) if before[key] else float(live[key]["initial_cash"])
        value=float(obs[-1]["equity"]) if complete else None
        entry={
            "track_id":key,"symbol":t["symbol"],"strategy":t["label"],
            "complete_observations":complete,
            "observed_days":len(got),"required_days":len(required),
            "missing_days":len(set(required)-set(got)),
            "log_net_return":float(__import__("math").log(value/initial)) if complete else None,
            "observed_net_return":(value/initial-1) if complete else None,
            "risk_halted":bool(live[key]["halted"]),
        }
        evaluations.append(entry)
        if t["label"]==frozen["benchmark_strategy_label"]:
            by_symbol_hold[t["symbol"]]=entry
    for item in evaluations:
        baseline=by_symbol_hold.get(item["symbol"])
        comparable=baseline and item["complete_observations"] and baseline["complete_observations"]
        item["log_excess_vs_hold"]=(
            item["log_net_return"]-baseline["log_net_return"] if comparable else None
        )
        item["research_review_only"] = True
    incomplete=any(not x["complete_observations"] or x["risk_halted"] for x in evaluations)
    return {
        "status":"PREWINDOW_VIRTUAL_EXPOSURE_REVIEW" if prewindow_orders else
                 "INCOMPLETE_OR_HALTED" if incomplete else "OBSERVED_FOR_MANUAL_REVIEW",
        "protocol_sha256":frozen["sha256"],
        "strategies":evaluations,
        "observation_window":[first.isoformat(),last.isoformat()],
        "blinded_holdout":False,
        "prewindow_virtual_events":prewindow_events,
        "prewindow_virtual_orders":prewindow_orders,
        "opening_inventory_requires_manual_review":bool(prewindow_orders),
        "confirmatory_inference_valid":False,
        "real_orders_enabled":False,"eligible_for_promotion":False,
        "warning":"Prior research and publicly monitored interim performance prevent claiming independent blinded alpha proof.",
    }

"""Read-only prospective protocol compliance and forward collection coverage.

No outcome/PNL in the monitoring payload. Does not modify or backfill events.
All checks use the Git-versioned preregistration manifest and UTC dates.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .monitor import _read_db
from .prospective import ProtocolError, _hash_track, _utc, read_protocol
from .ledger_audit import audit_shadow_ledger

GRACE_UTC_HOUR = 1
GRACE_UTC_MINUTE = 20  # 10:20 KST, after Shadow Paper 09:15-10:00 retries


def verify_frozen_accounts(accounts, manifest: str | Path, *, now: datetime | None = None) -> dict:
    """Fail-closed identity guard for production daily shadow execution."""
    stamp = _utc(now)
    plan = read_protocol(manifest)
    created = datetime.fromisoformat(plan["created_at_utc"])
    if created.tzinfo is None or stamp < created.astimezone(timezone.utc):
        raise ProtocolError("System UTC time precedes protocol registration")
    expected = {t["track_id"]: t for t in plan["account_set"]}
    current = {a["track_id"]: a for a in accounts}
    if len(current) != len(accounts) or set(current) != set(expected):
        raise ProtocolError("Frozen strategy universe changed from protocol")
    if len({(t["symbol"], t["label"]) for t in plan["account_set"]}) != len(expected):
        raise ProtocolError("Duplicate symbol-strategy pair in protocol")
    for key, declared in expected.items():
        a = current[key]
        if a["symbol"] != declared["symbol"] or a["label"] != declared["label"]:
            raise ProtocolError("Account symbol or strategy label was modified")
        if _hash_track(a) != declared["spec_and_account_sha256"]:
            raise ProtocolError("Frozen strategy/settings fingerprint mismatch")
        if date.fromisoformat(a["frozen_bar"]) >= date.fromisoformat(
            plan["first_eligible_decision_utc"]
        ):
            raise ProtocolError("Registration decision horizon precedes no frozen bar")
    return {"status": "PASS", "protocol_sha256": plan["sha256"],
            "verified_accounts": len(current), "real_orders_enabled": False}


def _due_decision(current: datetime, first: date, last: date) -> date | None:
    today = current.date()
    after_grace = (current.hour, current.minute) >= (
        GRACE_UTC_HOUR, GRACE_UTC_MINUTE
    )
    # No missing-day alert before first scheduled same-day UTC close+grace.
    day = today - timedelta(days=1 if after_grace else 2)
    return min(day, last) if day >= first else None


def assess_collection(
    shadow_db: str | Path, manifest: str | Path, *, now: datetime | None = None,
) -> dict:
    """Produce status/coverage, never reveal interim equity/returns.

    Each due UTC daily decision must have exactly one event per locked track.
    Account tampering or financial ledger corruption makes this fail closed.
    """
    current = _utc(now)
    plan = read_protocol(manifest)
    first = date.fromisoformat(plan["first_eligible_decision_utc"])
    last = date.fromisoformat(plan["last_eligible_decision_utc"])
    opening = datetime.fromisoformat(plan["evaluation_not_before_utc"])
    ledger = audit_shadow_ledger(shadow_db, now=current)
    if ledger["status"] != "PASS":
        return {"status": "INVALID_LEDGER", "protocol_sha256": plan["sha256"],
                "issues": [{"code": "LEDGER_AUDIT_FAILED"}],
                "real_orders_enabled": False, "eligible_for_promotion": False}
    with _read_db(shadow_db) as con:
        con.execute("BEGIN")
        accounts = con.execute("SELECT * FROM shadow_accounts ORDER BY symbol,label").fetchall()
        events = con.execute(
            "SELECT track_id,decision_date,session_date,created_at "
            "FROM shadow_events WHERE decision_date>=? AND decision_date<=? "
            "ORDER BY track_id,decision_date",
            (first.isoformat(), last.isoformat()),
        ).fetchall()
        prewindow=con.execute(
            "SELECT track_id,decision_date,action FROM shadow_events "
            "WHERE decision_date<? ORDER BY track_id,decision_date",
            (first.isoformat(),),
        ).fetchall()
        con.execute("COMMIT")
    try:
        verified = verify_frozen_accounts(accounts, manifest, now=current)
    except (ProtocolError, KeyError, TypeError, ValueError):
        return {"status": "INVALID_PROTOCOL_IDENTITY", "protocol_sha256": plan["sha256"],
                "issues": [{"code": "PROTOCOL_ACCOUNT_IDENTITY_FAILED"}],
                "real_orders_enabled": False, "eligible_for_promotion": False}
    due = _due_decision(current, first, last)
    due_days = 0 if due is None else (due - first).days + 1
    expected = {
        (first + timedelta(days=i)).isoformat()
        for i in range(due_days)
    }
    by_id = {t["track_id"]: set() for t in plan["account_set"]}
    issues = []
    if prewindow:
        issues.append({
            "code":"PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY",
            "prewindow_events":len(prewindow),
            "prewindow_virtual_orders":sum(
                item["action"] in ("BUY","SELL") for item in prewindow
            ),
            "first_unexpected_decision_utc":min(
                item["decision_date"] for item in prewindow
            ),
        })
    for e in events:
        day = date.fromisoformat(e["decision_date"])
        when = datetime.fromisoformat(e["created_at"])
        if when.tzinfo is None or when.astimezone(timezone.utc) > current:
            issues.append({"code":"EVENT_TIMESTAMP_IN_FUTURE"})
        if e["session_date"] != (day + timedelta(days=1)).isoformat():
            issues.append({"code":"EVENT_SESSION_NOT_NEXT_UTC_DAY"})
        if e["track_id"] not in by_id:
            issues.append({"code":"EVENT_UNKNOWN_TRACK"})
            continue
        by_id[e["track_id"]].add(e["decision_date"])
    detail = []
    for t in plan["account_set"]:
        observed = by_id[t["track_id"]]
        missing = sorted(expected - observed)
        detail.append({
            "symbol": t["symbol"], "strategy": t["label"],
            "due_days": due_days, "observed_due_days": len(expected & observed),
            "missing_due_days": len(missing),
            "first_missing_utc_day": missing[0] if missing else None,
            # No cash/equity/returns, even after cutoff, from this endpoint.
        })
    halted_count = sum(bool(a["halted"]) for a in accounts)
    if halted_count:
        issues.append({"code": "PROTOCOL_RISK_HALTED",
                       "affected_accounts": halted_count})
    missing_total = sum(d["missing_due_days"] for d in detail)
    if missing_total:
        issues.append({"code": "PROTOCOL_DUE_OBSERVATIONS_MISSING",
                       "affected_accounts": sum(d["missing_due_days"] > 0 for d in detail),
                       "missing_account_days": missing_total})
    if issues:
        status = "BREACH_OR_MISSING"
    elif current >= opening:
        status = "WINDOW_CLOSED_PENDING_MANUAL_REVIEW"
    elif due_days == 0:
        status = "REGISTERED_WAITING_FIRST_DUE"
    else:
        status = "COLLECTING_AS_PREDECLARED"
    return {
        "status": status,
        "protocol_sha256": verified["protocol_sha256"],
        "first_eligible_decision_utc": first.isoformat(),
        "last_eligible_decision_utc": last.isoformat(),
        "evaluation_not_before_utc": opening.isoformat(),
        "latest_due_decision_utc": due.isoformat() if due else None,
        "expected_due_days_per_account": due_days,
        "accounts": detail,
        "issues": issues,
        "blinded_holdout": False,
        "includes_return_metrics": False,
        "real_orders_enabled": False,
        "eligible_for_promotion": False,
    }

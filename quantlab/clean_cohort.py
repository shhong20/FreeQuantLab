"""Independent, fail-closed monitoring of a clean preregistered 90-day cohort.

Read-only on the cohort ledger. All accounts start with flat inventory; historical
market candles cannot be substituted for virtual orders. Never reveal returns,
unlock capital, or promote strategies. The alert database is independent.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys

from .daily_receipt import _safe_report, KST
from .ledger_audit import audit_shadow_ledger
from .monitor import _read_db
from .prospective import _utc, read_protocol
from .protocol_compliance import assess_collection
from .quote_quality import execution_proxy_quality
from .watchdog import persist_alert_transitions, deliver_pending, export_alert_history

_EXPECTED = {"KRW-BTC", "KRW-ETH"}
_STRATEGIES = {"buy_hold_20pct", "trend_ma20_80", "factor_r5_gt0"}


def inspect_clean(
    shadow_db: str | Path, manifest: str | Path, *,
    now: datetime | None = None,
) -> dict:
    current = _utc(now)
    plan = read_protocol(manifest)
    first = date.fromisoformat(plan["first_eligible_decision_utc"])
    last = date.fromisoformat(plan["last_eligible_decision_utc"])
    collection = assess_collection(shadow_db, manifest, now=current)
    ledger = audit_shadow_ledger(shadow_db, now=current)
    proxy = execution_proxy_quality(shadow_db)
    issues: set[str] = set()
    if ledger["status"] != "PASS":
        issues.add("LEDGER_AUDIT_FAILED")
    if proxy["status"] not in {"PASS", "AWAITING_FIRST_OBSERVATION"}:
        issues.add("QUOTE_EVIDENCE_AUDIT_FAILED")
    if collection["status"] in {"INVALID_LEDGER", "INVALID_PROTOCOL_IDENTITY", "BREACH_OR_MISSING"}:
        issues.add("FROZEN_PROTOCOL_COMPLIANCE_FAILED")
    if plan["required_complete_utc_days"] != 90 or len(plan["account_set"]) != 6:
        issues.add("CLEAN_90D_PROTOCOL_UNEXPECTED")
    if {(t["symbol"], t["label"]) for t in plan["account_set"]} != {
        (symbol, strategy) for symbol in _EXPECTED for strategy in _STRATEGIES
    }:
        issues.add("FROZEN_STRATEGY_SET_UNEXPECTED")

    with _read_db(shadow_db) as con:
        con.execute("BEGIN")
        accounts = con.execute(
            "SELECT track_id,symbol,label,initial_cash,cash,units,registered_utc,"
            "frozen_bar FROM shadow_accounts"
        ).fetchall()
        events = con.execute(
            "SELECT e.track_id,a.symbol,e.decision_date,e.session_date,e.created_at,"
            "e.observed_price FROM shadow_events e "
            "JOIN shadow_accounts a ON a.track_id=e.track_id"
        ).fetchall()
        quotes = con.execute("SELECT * FROM shadow_quote_evidence").fetchall()
        con.execute("COMMIT")

    # Empty opening inventory is a property of the new DB at registration.
    # A first valid virtual BUY after the frozen start is not a violation.
    if len(accounts) != 6 or any(float(a["initial_cash"]) != 1_000_000 for a in accounts):
        issues.add("UNEXPECTED_OPENING_CAPITAL_OR_ACCOUNT_SET")
    if any(date.fromisoformat(a["frozen_bar"]) >= first for a in accounts):
        issues.add("REGISTRATION_BAR_NOT_BEFORE_EVALUATION")
    if any(
        (date.fromisoformat(e["decision_date"]) < first or
         date.fromisoformat(e["decision_date"]) > last)
        for e in events
    ):
        issues.add("OUTSIDE_WINDOW_VIRTUAL_ACTIVITY")
    # A future day's observation cannot be accepted as a completed candle.
    if any(date.fromisoformat(e["decision_date"]) >= current.date() for e in events):
        issues.add("UNFINISHED_UTC_CANDLE_ACTIVITY")

    quote_map = {(q["symbol"], q["decision_date"]): q for q in quotes}
    events_map = {(e["track_id"], e["decision_date"]): e for e in events}
    due_days = collection.get("expected_due_days_per_account", 0)
    expected, observed, bad = 0, 0, 0
    first_problem = None
    for offset in range(due_days):
        day = first + timedelta(days=offset)
        dec, session = day.isoformat(), (day + timedelta(days=1)).isoformat()
        for a in accounts:
            expected += 1
            e = events_map.get((a["track_id"], dec))
            if e is None:
                if first_problem is None:
                    first_problem = dec
                continue
            observed += 1
            q = quote_map.get((a["symbol"], dec))
            valid = bool(q) and e["session_date"] == session
            try:
                saved = datetime.fromisoformat(e["created_at"])
                valid = valid and saved.tzinfo is not None
                if saved.tzinfo is not None:
                    stamp = saved.astimezone(timezone.utc)
                    valid = (valid and stamp.date().isoformat() == session
                             and (0, 10) <= (stamp.hour, stamp.minute) < (1, 20)
                             and stamp <= current)
                valid = valid and q["source"] == "upbit_public_ticker" if q is not None else False
                if q is not None:
                    qtime = datetime.fromisoformat(q["observed_at_utc"])
                    valid = valid and qtime.tzinfo is not None
                    if qtime.tzinfo is not None:
                        valid = valid and abs((saved-qtime).total_seconds()) <= 120
                    valid = valid and math.isclose(
                        float(e["observed_price"]), float(q["public_ticker_price"]),
                        rel_tol=1e-10, abs_tol=1e-7,
                    )
            except (TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                bad += 1
                if first_problem is None:
                    first_problem = dec

    if observed != expected:
        issues.add("CUMULATIVE_DUE_ACCOUNT_DAYS_MISSING")
    if bad:
        issues.add("CUMULATIVE_QUOTE_OR_TIMESTAMP_EVIDENCE_INVALID")
    # No orphan quote or event should survive as "healthy", even when before due.
    if any((e["symbol"], e["decision_date"]) not in quote_map for e in events):
        issues.add("MISSING_PUBLIC_QUOTE")
    if any(not any(e["symbol"] == q["symbol"] and e["decision_date"] == q["decision_date"]
                   for e in events) for q in quotes):
        issues.add("ORPHAN_PUBLIC_QUOTE")

    waiting = due_days == 0
    return {
        "status": "INCOMPLETE" if issues else "WAITING_FIRST_DUE" if waiting else "PASS",
        "checked_at_utc": current.isoformat(),
        "kst_receipt_date": current.astimezone(KST).date().isoformat(),
        "protocol_sha256": plan["sha256"],
        "first_eligible_decision_utc": first.isoformat(),
        "last_eligible_decision_utc": last.isoformat(),
        "evaluation_not_before_utc": plan["evaluation_not_before_utc"],
        "expected_due_account_days": expected,
        "recorded_due_account_days": observed,
        "invalid_due_account_days": bad,
        "first_incomplete_decision_utc": first_problem,
        "protocol_collection_status": collection["status"],
        "quote_proxy_status": proxy["status"],
        "ledger_audit_status": ledger["status"],
        "issues": sorted(issues),
        "original_legacy_cohort_replaced": False,
        "all_virtual_only": True,
        "strategy_returns": None,
        "real_orders_enabled": False,
        "automatic_strategy_promotion": False,
        "blinded_holdout": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean 90-day cohort independent read-only acceptance")
    parser.add_argument("--db", default="state/clean_shadow_90d.db")
    parser.add_argument("--protocol", default="protocols/2026-10-21-btc-eth-clean-90d.json")
    parser.add_argument("--alert-db", default="state/clean-watchdog.db")
    parser.add_argument("--report-dir", default="reports/clean-daily")
    args = parser.parse_args(argv)
    try:
        result = inspect_clean(args.db, args.protocol)
        alert_list = [{
            "severity": "critical",
            "code": "CLEAN_COHORT_" + issue,
            "message": "Clean forward research cohort requires investigation: " + issue,
        } for issue in result["issues"]]
        stamp = datetime.fromisoformat(result["checked_at_utc"])
        transition = persist_alert_transitions(args.alert_db, alert_list, now=stamp)
        deliver_pending(args.alert_db, toast=False, now=stamp)
        export_alert_history(args.alert_db,
                             Path(args.alert_db).with_name("clean-watchdog-history.json"),
                             now=stamp)
        report_file = Path(args.report_dir) / (result["kst_receipt_date"] + ".json")
        _safe_report(report_file, result)
        print(json.dumps({"status":result["status"],"report":str(report_file),
                          "active_alerts":transition["active_alerts"],
                          "expected_due_account_days":result["expected_due_account_days"],
                          "issues":result["issues"],"strategy_returns":None,
                          "real_orders_enabled":False}, ensure_ascii=False))
        return 0 if result["status"] != "INCOMPLETE" else 2
    except (ValueError, RuntimeError, KeyError, OSError, sqlite3.Error) as exc:
        print("CLEAN COHORT FAILED CLOSED: " + type(exc).__name__ + ": " + str(exc)[:180], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

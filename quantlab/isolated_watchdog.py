"""Independent read-only monitoring of automatically observed isolated research labs.

Checks future UTC decision coverage, frozen plan/ledger/quote validity and exact
Qlib model replay. Never backfills missed days, modifies paper accounts or
promotes strategies. Uses separate durable alerts/report paths from original
90-day watchdog.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys

from .isolated_scheduler import DEFAULT_POLICY, VALID_LANES, read_schedule, verify_lane
from .monitor import _read_db
from .prospective import _utc
from .protocol_compliance import _due_decision
from .watchdog import (
    persist_alert_transitions, deliver_pending, export_alert_history, _atomic
)


def inspect_isolated(
    *, policy_path: str | Path = DEFAULT_POLICY, now: datetime | None = None,
    verify_func=verify_lane,
) -> dict:
    current = _utc(now)
    policy = read_schedule(policy_path)
    first = __import__("datetime").date.fromisoformat(policy["first_decision_utc"])
    last = __import__("datetime").date.fromisoformat(policy["last_decision_utc"])
    due = _due_decision(current, first, last)
    due_dates = [] if due is None else [
        (first + timedelta(days=i)).isoformat()
        for i in range((due - first).days + 1)
    ]
    alerts = []
    details = []
    for name in VALID_LANES:
        item = policy["lanes"][name]
        try:
            verify_func(policy, name, now=current)
            with _read_db(item["db"]) as c:
                c.execute("BEGIN")
                rows = c.execute(
                    "SELECT a.symbol,a.label,e.decision_date,e.session_date,e.created_at "
                    "FROM shadow_accounts a LEFT JOIN shadow_events e "
                    "ON e.track_id=a.track_id ORDER BY a.symbol,e.decision_date"
                ).fetchall()
                runs = c.execute(
                    "SELECT status,created_at FROM shadow_runs "
                    "ORDER BY run_id DESC LIMIT 1"
                ).fetchone()
                c.execute("COMMIT")
            got = {sym: set() for sym in ("KRW-BTC", "KRW-ETH")}
            anomalous = set()
            for row in rows:
                if row["symbol"] not in got:
                    raise ValueError("Unexpected research account symbol")
                day = row["decision_date"]
                if day:
                    got[row["symbol"]].add(day)
                    if row["session_date"] != (
                        __import__("datetime").date.fromisoformat(day)
                        + timedelta(days=1)
                    ).isoformat():
                        anomalous.add((row["symbol"], day))
            due_set = set(due_dates)
            for symbol in ("KRW-BTC", "KRW-ETH"):
                absent = sorted(due_set - got[symbol])
                details.append({
                    "lane": name, "symbol": symbol, "due_utc_days": len(due_dates),
                    "recorded_due_days": len(due_set & got[symbol]),
                    "missing_due_days": len(absent),
                    "first_missing_utc_day": absent[0] if absent else None,
                })
                if absent:
                    alerts.append({
                        "severity": "warning", "code": "ISOLATED_DUE_DAY_MISSING",
                        "symbol": symbol, "strategy": name,
                        "message": f"{name} {symbol} missed {len(absent)} due UTC daily paper observations; no backfill permitted.",
                    })
            if anomalous:
                alerts.append({
                    "severity": "critical", "code": "ISOLATED_EVENT_SESSION_INVALID",
                    "strategy": name,
                    "message": f"{name} has invalid event/session dates.",
                })
            if runs is not None and runs["status"] == "ERROR":
                alerts.append({
                    "severity": "warning", "code": "ISOLATED_LAST_CYCLE_ERROR",
                    "strategy": name,
                    "message": f"Latest {name} frozen paper cycle reported ERROR.",
                })
        except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, TypeError) as exc:
            # Do not surface private filesystem paths or model payloads in alerts.
            alerts.append({
                "severity": "critical", "code": "ISOLATED_LANE_INTEGRITY_FAILED",
                "strategy": name,
                "message": f"Frozen {name} paper lane plan/ledger/model audit failed: {type(exc).__name__}.",
            })
            for symbol in ("KRW-BTC", "KRW-ETH"):
                details.append({
                    "lane": name, "symbol": symbol, "due_utc_days": len(due_dates),
                    "recorded_due_days": None, "missing_due_days": None,
                    "first_missing_utc_day": None,
                })
    return {
        "status": "DEGRADED" if any(a["severity"] == "critical" for a in alerts)
                  else "WARNING" if alerts else
                  "WAITING_FIRST_DUE" if not due_dates else "COLLECTING",
        "checked_at_utc": current.isoformat(),
        "automation_policy_sha256": policy["sha256"],
        "due_utc_day": due.isoformat() if due else None,
        "coverage": details,
        "alerts": alerts,
        "no_observed_returns_exposed": True,
        "virtual_only": True, "real_orders_enabled": False,
    }


def check_isolated(
    *, policy_path: str | Path = DEFAULT_POLICY,
    state_db: str | Path = "state/isolated-watchdog.db",
    report_dir: str | Path = "reports/isolated",
    now: datetime | None = None,
    windows_notifications: bool = False,
    verify_func=verify_lane,
) -> dict:
    current = _utc(now)
    result = inspect_isolated(
        policy_path=policy_path, now=current, verify_func=verify_func,
    )
    transitions = persist_alert_transitions(state_db, result["alerts"], now=current)
    delivery = deliver_pending(state_db, toast=windows_notifications, now=current)
    inbox = export_alert_history(
        state_db, Path(state_db).with_name(Path(state_db).stem + "-history.json"),
        now=current,
    )
    target = Path(report_dir) / (current.date().isoformat() + ".json")
    _atomic(target, (json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
                     + "\n").encode("utf-8"))
    return {
        "status": result["status"],
        "checked_at_utc":current.isoformat(),
        "active_alerts":transitions["active_alerts"],
        "alert_transitions":transitions["transitions"],
        "inbox_snapshot":str(inbox),
        "report":str(target),
        "windows_delivery":delivery,
        "real_orders_enabled":False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Independent isolated virtual lab monitor")
    parser.add_argument("--policy", default=DEFAULT_POLICY)
    parser.add_argument("--state-db", default="state/isolated-watchdog.db")
    parser.add_argument("--report-dir", default="reports/isolated")
    parser.add_argument("--windows-toast", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check_isolated(
            policy_path=args.policy, state_db=args.state_db,
            report_dir=args.report_dir, windows_notifications=args.windows_toast,
        )
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result["status"] == "DEGRADED" or (
            result["windows_delivery"]["delivery_failed"] > 0
        ) else 0
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as exc:
        print("ISOLATED WATCHDOG FAILED CLOSED: "
              + type(exc).__name__ + ": " + str(exc)[:160], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Independent operational alerts for lost 10-minute public market sampling.

This is NOT the underlying raw-SHA, WS-causality, market-depth or real-fill
quality gate. It never backfills missing snapshots or resolves upstream
independent quality services on behalf of the operator.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from .ops_integrity import PUBLIC_DB_DIR, audit_slot_window
from .watchdog import deliver_pending, export_alert_history, persist_alert_transitions

ALERT_DB = "/home/hong/apps/FreeQuantLab/state/market-gap-alerts.db"
REPORT = "/home/hong/apps/FreeQuantLab/reports/market-quality/gap-alert-history.json"
MAX_TOLERATED_CONSECUTIVE_MISSING = 3
MIN_ACCEPTABLE_SLOT_FRACTION = 0.90


def inspect_gap_alerts(l2: str | Path, websocket: str | Path, *,
                       now: datetime | None = None) -> tuple[dict, list[dict]]:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("Timestamp must be aware")
    data = {
        "l2": audit_slot_window(l2, "l2", now=stamp),
        "websocket": audit_slot_window(websocket, "websocket", now=stamp),
    }
    alerts = []
    for market, info in data.items():
        missed = info["missing_due_slots"]
        if missed <= 0:
            continue
        degraded = (
            info["coverage_fraction"] < MIN_ACCEPTABLE_SLOT_FRACTION
            or info["longest_missing_run_slots"] > MAX_TOLERATED_CONSECUTIVE_MISSING
        )
        severity = "critical" if degraded else "warning"
        alerts.append({
            "code": "MARKET_" + market.upper() + "_ROLLING_SLOT_GAP",
            "severity": severity,
            "message": (
                f"{market} missing={missed}/{info['expected_slots']}, "
                f"max_consecutive={info['longest_missing_run_slots']}; "
                "past evidence cannot be recreated; independent SHA/quality gate "
                "remains a separate required check."
            ),
        })
    return {
        "status": (
            "CAPTURE_SLOTS_MISSING_NO_RETROACTIVE_REPAIR" if alerts
            else "CAPTURE_SLOTS_PRESENT_QUALITY_NOT_YET_CERTIFIED"
        ),
        "checked_at_utc": stamp.astimezone(timezone.utc).isoformat(),
        "streams": data,
        "source_database_read_only": True,
        "last_24h_slots_complete": not bool(alerts),
        "independent_raw_sha_and_quality_gate_approved": False,
        "past_missing_market_data_backfilled": False,
        "true_order_fills_verified": False,
        "real_orders_enabled": False,
        "automatic_strategy_promotion": False,
    }, alerts


def run_gap_monitor(l2: str | Path, websocket: str | Path,
                    state_db: str | Path, report: str | Path, *,
                    now: datetime | None = None, toast: bool = False) -> dict:
    stamp = now or datetime.now(timezone.utc)
    # Inspect both sources before writing an alert transition. A source
    # failure must never falsely resolve a prior active incident.
    snapshot, alerts = inspect_gap_alerts(l2, websocket, now=stamp)
    transitions = persist_alert_transitions(state_db, alerts, now=stamp)
    delivery = deliver_pending(state_db, toast=toast, now=stamp)
    exported = export_alert_history(state_db, report, now=stamp)
    return {
        **snapshot,
        "alert_transition_count": len(transitions["transitions"]),
        "active_gap_alerts": transitions["active_alerts"],
        "history_file": str(exported),
        "submitted_to_windows_api": delivery["submitted_to_windows_api"],
        "windows_notification_delivery_verified": False,
        "standalone_quality_gate_result_altered": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Persist independent L2/WS slot-gap alerts; never approve execution"
    )
    parser.add_argument("--l2", default=str(PUBLIC_DB_DIR / "observations.sqlite3"))
    parser.add_argument("--websocket", default=str(PUBLIC_DB_DIR / "websocket.sqlite3"))
    parser.add_argument("--alert-db", default=ALERT_DB)
    parser.add_argument("--report", default=REPORT)
    parser.add_argument("--windows-toast", action="store_true")
    args = parser.parse_args(argv)
    try:
        response = run_gap_monitor(
            args.l2, args.websocket, args.alert_db, args.report,
            toast=args.windows_toast,
        )
        print(json.dumps(response, sort_keys=True, ensure_ascii=False))
        # Successful monitor execution does not certify a 24h quality gate.
        return 0
    except (OSError, sqlite3.Error, ValueError, KeyError,
            RuntimeError, TypeError) as e:
        print(json.dumps({
            "status": "MARKET_GAP_MONITOR_UNAVAILABLE_EXISTING_ALERTS_PRESERVED",
            "reason": type(e).__name__,
            "market_quality_verified": False,
            "real_orders_enabled": False,
        }), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

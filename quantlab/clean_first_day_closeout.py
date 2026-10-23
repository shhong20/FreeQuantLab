"""Read-only first-day closeout across the frozen ledger, SHA receipt and backup.

This module never issues orders, creates a receipt, fills missing market
observations, rewrites a snapshot or calls an external timestamp service.
A successful local closeout does not prove returns or independent offhost data.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

from .clean_backup import BackupError, DEFAULT_TARGET, _hash, verify_backup
from .clean_cohort import inspect_clean
from .clean_first_day_attestation import (
    AdmissionError, DEFAULT_DB, DEFAULT_PROTOCOL, DEFAULT_RECEIPT_DIR,
    _digest, _first_day_snapshot,
)
from .prospective import _utc, read_protocol


def inspect_closeout(db: str | Path = DEFAULT_DB,
                     protocol: str | Path = DEFAULT_PROTOCOL,
                     receipt_dir: str | Path = DEFAULT_RECEIPT_DIR,
                     backup_dir: str | Path = DEFAULT_TARGET,
                     *, now: datetime | None = None) -> dict:
    current = _utc(now)
    plan = read_protocol(protocol)
    first = date.fromisoformat(plan["first_eligible_decision_utc"])
    due = datetime.combine(first + timedelta(days=1), time(1, 20), timezone.utc)
    watchdog = datetime.combine(first + timedelta(days=1), time(1, 50), timezone.utc)
    backup_due = datetime.combine(first + timedelta(days=1), time(2, 30), timezone.utc)
    base = {
        "protocol_sha256": plan["sha256"],
        "first_eligible_decision_utc": first.isoformat(),
        "checked_at_utc": current.isoformat(),
        "read_only_verification": True,
        "receipt_created_by_this_command": False,
        "backup_created_by_this_command": False,
        "synthetic_observations_created": False,
        "real_orders_enabled": False,
        "automatic_strategy_promotion": False,
        "offhost_copy_verified": False,
        "execution_quality_or_returns_certified": False,
    }
    if current < due:
        return {**base, "status": "WAITING_FIRST_DAY_MATURITY"}
    receipt = Path(receipt_dir) / ("first-day-" + first.isoformat() + ".json")
    if not receipt.is_file():
        return {**base, "status": (
            "WAITING_INDEPENDENT_WATCHDOG" if current < watchdog
            else "BLOCKED_FIRST_DAY_RECEIPT_MISSING")}
    doc = json.loads(receipt.read_text("utf-8"))
    if (doc.get("status") != "FIRST_DAY_COMPLETE_VERIFIED_NO_PERFORMANCE_CLAIM" or
        doc.get("protocol_sha256") != plan["sha256"] or
        doc.get("first_day_account_day_events") != 6 or
        doc.get("first_day_public_quote_evidence") != 2):
        raise AdmissionError("Existing first-day receipt is invalid")
    report = inspect_clean(db, protocol, now=current)
    if (report["status"] != "PASS" or report["issues"] or
        report["ledger_audit_status"] != "PASS" or
        report["quote_proxy_status"] != "PASS"):
        raise AdmissionError("Live cohort does not match independent admission")
    events, quotes = _first_day_snapshot(Path(db), first.isoformat(), plan)
    if (doc["first_day_events_sha256"] != _digest(events) or
        doc["first_day_public_quotes_sha256"] != _digest(quotes)):
        raise AdmissionError("Live first-day evidence differs from immutable receipt")
    digest = _hash(receipt)
    root = Path(backup_dir)
    snapshots = sorted(root.glob("snapshot-*"), reverse=True) if root.is_dir() else []
    matching = None
    for folder in snapshots:
        manifest_file = folder / "manifest.json"
        if not manifest_file.is_file():
            continue
        manifest = json.loads(manifest_file.read_text("utf-8"))
        captured = datetime.fromisoformat(manifest["captured_at_utc"]).astimezone(timezone.utc)
        if captured < due:
            continue
        # A later attempted backup missing the receipt must not be hidden
        # by falling back to an older successful snapshot.
        if manifest.get("first_day_receipt_sha256") != digest:
            raise BackupError("Post-attestation snapshot omits or mismatches receipt")
        result = verify_backup(folder, protocol)
        if not result["first_day_receipt_present_and_matched"]:
            raise BackupError("Post-attestation restored snapshot lacks receipt")
        matching = {
            "snapshot": str(folder),
            "snapshot_sqlite_sha256": manifest["sqlite_sha256"],
            "receipt_sha256": digest,
            "backed_up_accounts": manifest["record_counts"]["shadow_accounts"],
            "backed_up_events": manifest["record_counts"]["shadow_events"],
            "backed_up_quotes": manifest["record_counts"]["shadow_quote_evidence"],
        }
        break
    if matching is None:
        return {**base,
                "status": ("WAITING_POST_AUDIT_BACKUP" if current < backup_due
                           else "BLOCKED_POST_AUDIT_BACKUP_MISSING"),
                "first_day_live_receipt_verified": True}
    return {
        **base, **matching,
        "status": "LOCAL_FIRST_DAY_RECEIPT_AND_BACKUP_MATCH",
        "first_day_live_receipt_verified": True,
        "post_attestation_backup_restored_readonly": True,
        "first_day_event_count": 6,
        "first_day_quote_count": 2,
        "performance_claim": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only closeout: live clean evidence, sealed first-day SHA, local restored backup")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--protocol", default=DEFAULT_PROTOCOL)
    parser.add_argument("--receipt-dir", default=DEFAULT_RECEIPT_DIR)
    parser.add_argument("--backup-dir", default=DEFAULT_TARGET)
    options = parser.parse_args(argv)
    try:
        result = inspect_closeout(options.db, options.protocol,
                                  options.receipt_dir, options.backup_dir)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0 if result["status"] == "LOCAL_FIRST_DAY_RECEIPT_AND_BACKUP_MATCH" else 2
    except (OSError, ValueError, KeyError, AdmissionError, BackupError,
            TypeError, sqlite3.Error, RuntimeError) as exc:
        print(json.dumps({
            "status": "CLEAN_FIRST_DAY_CLOSEOUT_FAIL_CLOSED",
            "failure_type": type(exc).__name__,
            "market_data_backfilled": False,
            "receipt_created": False,
            "real_orders_enabled": False,
        }, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

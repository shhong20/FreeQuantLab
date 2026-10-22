"""Consistent, local-only backups for the immutable clean 90-day research ledger.

SQLite's online backup API copies a consistent read snapshot (including WAL).
This module never repairs a trading ledger, generates virtual fills, exposes
broker orders, or asserts an offsite or third-party timestamp attestation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, time, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

from .clean_first_day_attestation import _digest, _first_day_snapshot
from .ledger_audit import audit_shadow_ledger
from .monitor import _read_db
from .prospective import read_protocol

DEFAULT_DB = "/home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db"
DEFAULT_PROTOCOL = ("/home/hong/apps/FreeQuantLab-clean-runtime/"
                    "protocols/2026-10-21-btc-eth-clean-90d.json")
DEFAULT_RECEIPT_DIR = "/home/hong/apps/FreeQuantLab/reports/clean-daily/first-day"
DEFAULT_TARGET = "/mnt/d/FreeQuantLab-Backups/clean-90d"
SCHEMA = 1


class BackupError(ValueError):
    pass


def _hash(path: Path) -> str:
    h = sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_counts(db: Path) -> dict:
    with closing(_read_db(db)) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("Saved SQLite integrity_check failed")
        return {table: int(con.execute("SELECT count(*) FROM " + table).fetchone()[0])
                for table in ("shadow_accounts", "shadow_events", "shadow_quote_evidence")}


def verify_backup(bundle: str | Path, protocol: str | Path) -> dict:
    directory = Path(bundle).resolve(strict=True)
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    plan = read_protocol(protocol)
    if manifest.get("schema") != SCHEMA or manifest.get("protocol_sha256") != plan["sha256"]:
        raise BackupError("Backup manifest or frozen protocol identity mismatch")
    db = directory / "clean_shadow_90d.sqlite3"
    if _hash(db) != manifest["sqlite_sha256"]:
        raise BackupError("Saved SQLite SHA-256 changed")
    counts = _snapshot_counts(db)
    if counts != manifest["record_counts"] or counts["shadow_accounts"] != 6:
        raise BackupError("Backup cohort account or record counts mismatch")
    ledger = audit_shadow_ledger(db)
    if ledger["status"] != "PASS":
        raise BackupError("Ledger replay audit of snapshot failed")
    receipt = manifest.get("first_day_receipt_sha256")
    if receipt is not None:
        item = directory / "first-day.json"
        if _hash(item) != receipt:
            raise BackupError("First-day receipt SHA-256 changed")
        doc = json.loads(item.read_text("utf-8"))
        first = plan["first_eligible_decision_utc"]
        if (doc.get("protocol_sha256") != plan["sha256"] or
            doc.get("status") != "FIRST_DAY_COMPLETE_VERIFIED_NO_PERFORMANCE_CLAIM"):
            raise BackupError("First-day receipt does not belong to this protocol")
        events, quotes = _first_day_snapshot(db, first, plan)
        if (doc["first_day_events_sha256"] != _digest(events) or
            doc["first_day_public_quotes_sha256"] != _digest(quotes)):
            raise BackupError("First-day evidence does not match restored SQLite snapshot")
    return {
        "status": "CONSISTENT_BACKUP_RESTORED_READONLY",
        "source_db_modified": False,
        "events": counts["shadow_events"],
        "quotes": counts["shadow_quote_evidence"],
        "first_day_receipt_present_and_matched": receipt is not None,
        "offhost_independent_copy_proven": False,
        "live_execution_verified": False,
    }


def create_backup(db: str | Path, protocol: str | Path,
                  receipt_dir: str | Path, target: str | Path,
                  *, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise BackupError("Timezone-aware current time required")
    plan = read_protocol(protocol)
    first_day = date.fromisoformat(plan["first_eligible_decision_utc"])
    receipt_expected = current.astimezone(timezone.utc) >= datetime.combine(
        first_day + timedelta(days=1),time(1,20),timezone.utc)
    if (plan["required_complete_utc_days"] != 90 or len(plan["account_set"]) != 6 or
        plan["real_broker_order_permission"] is not False or
        plan["automatic_strategy_promotion"] is not False):
        raise BackupError("Non-clean or unexpectedly mutable protocol")
    source = Path(db).resolve(strict=True)
    if source.name != "clean_shadow_90d.db":
        raise BackupError("Only dedicated clean 90-day SQLite ledger supported")
    parent = Path(target).resolve()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = parent / ("snapshot-" + current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    folder.mkdir(mode=0o700, exist_ok=False)
    snapshot = folder / "clean_shadow_90d.sqlite3"
    prior_umask = os.umask(0o077)
    try:
        with closing(_read_db(source)) as src:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst, pages=128, sleep=0.05)
        counts = _snapshot_counts(snapshot)
        if counts["shadow_accounts"] != 6 or audit_shadow_ledger(snapshot)["status"] != "PASS":
            raise BackupError("Backup does not pass frozen ledger accounting")
        first = plan["first_eligible_decision_utc"]
        receipt_path = Path(receipt_dir) / ("first-day-" + first + ".json")
        evidence_sha = None
        if receipt_path.is_file():
            copy = folder / "first-day.json"
            with receipt_path.open("rb") as r, copy.open("xb") as w:
                shutil.copyfileobj(r, w)
            evidence_sha = _hash(copy)
        manifest = {
            "schema": SCHEMA,
            "captured_at_utc": current.astimezone(timezone.utc).isoformat(),
            "protocol_sha256": plan["sha256"],
            "sqlite_sha256": _hash(snapshot),
            "first_day_receipt_sha256": evidence_sha,
            "first_day_receipt_expected_by_capture": receipt_expected,
            "record_counts": counts,
            "source_is_forward_research_only": True,
            "real_orders_enabled": False,
            "strategy_auto_promotion": False,
            "third_party_timestamp_or_offhost_verification": False,
        }
        with (folder / "manifest.json").open("x", encoding="utf-8") as out:
            json.dump(manifest, out, sort_keys=True, separators=(",", ":"))
        restored = verify_backup(folder, protocol)
        verified = evidence_sha is not None or not receipt_expected
        return {"status": ("CONSISTENT_LOCAL_BACKUP_VERIFIED" if verified else
                           "LOCAL_BACKUP_MISSING_DUE_FIRST_DAY_RECEIPT"),
                "snapshot_directory": str(folder),
                "sqlite_sha256": manifest["sqlite_sha256"],
                "first_day_receipt_attached": restored["first_day_receipt_present_and_matched"],
                "record_counts": counts, "real_orders_enabled": False,
                "offsite_backup": False}
    finally:
        os.umask(prior_umask)


def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser(description="Consistent local-only clean 90-day snapshot + restore check")
    ap.add_argument("--db",default=DEFAULT_DB)
    ap.add_argument("--protocol",default=DEFAULT_PROTOCOL)
    ap.add_argument("--receipt-dir",default=DEFAULT_RECEIPT_DIR)
    ap.add_argument("--target",default=DEFAULT_TARGET)
    args=ap.parse_args(argv)
    try:
        result=create_backup(args.db,args.protocol,args.receipt_dir,args.target)
        print(json.dumps(result,sort_keys=True))
        return 0 if result["status"] == "CONSISTENT_LOCAL_BACKUP_VERIFIED" else 2
    except (OSError,RuntimeError,ValueError,KeyError,sqlite3.Error,TypeError) as exc:
        print(json.dumps({"status":"CLEAN_BACKUP_BLOCKED",
                          "reason":type(exc).__name__,
                          "never_auto_repair_original":True}),file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

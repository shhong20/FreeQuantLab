"""Synthetic evidence end-to-end, with no live cohort or receipt modification."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from test_clean_cohort import cohort, record, DUE
from quantlab.clean_first_day_attestation import attest_first_day, AdmissionError
from quantlab.clean_backup import BackupError, create_backup
from quantlab.clean_first_day_closeout import inspect_closeout


def _shadow(db: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "clean_shadow_90d.db"
    with sqlite3.connect(db) as src, sqlite3.connect(copy) as dst:
        src.backup(dst)
    return copy


def test_waiting_for_maturity_does_not_write_or_approve(cohort, tmp_path):
    db, manifest = cohort
    clean = _shadow(db, tmp_path)
    before = clean.read_bytes()
    result = inspect_closeout(
        clean, manifest, tmp_path/"not-existing", tmp_path/"backup",
        now=datetime(2026,10,23,1,0,tzinfo=timezone.utc),
    )
    assert result["status"] == "WAITING_FIRST_DAY_MATURITY"
    assert clean.read_bytes() == before
    assert not result["receipt_created_by_this_command"]


def test_no_receipt_after_watchdog_is_blocked_without_creating_it(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    clean=_shadow(db,tmp_path)
    receipts=tmp_path/"receipts"
    result=inspect_closeout(clean,manifest,receipts,tmp_path/"backups",
                            now=datetime(2026,10,23,2,0,tzinfo=timezone.utc))
    assert result["status"]=="BLOCKED_FIRST_DAY_RECEIPT_MISSING"
    assert not receipts.exists()


def test_live_seal_and_restored_snapshot_match(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    clean=_shadow(db,tmp_path)
    receipts=tmp_path/"receipts"
    sealed=attest_first_day(clean,manifest,receipts,now=DUE)
    assert sealed["receipt_created"]
    waiting=inspect_closeout(clean,manifest,receipts,tmp_path/"backups",now=DUE)
    assert waiting["status"]=="WAITING_POST_AUDIT_BACKUP"
    b=create_backup(clean,manifest,receipts,tmp_path/"backups",now=DUE)
    assert b["first_day_receipt_attached"]
    done=inspect_closeout(clean,manifest,receipts,tmp_path/"backups",now=DUE)
    assert done["status"]=="LOCAL_FIRST_DAY_RECEIPT_AND_BACKUP_MATCH"
    assert done["first_day_event_count"]==6
    assert done["first_day_quote_count"]==2
    assert done["receipt_sha256"]==sealed["receipt_sha256"]
    assert done["post_attestation_backup_restored_readonly"]
    assert not done["offhost_copy_verified"]


def test_tampered_saved_backup_refused_without_fallback(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    clean=_shadow(db,tmp_path)
    receipts=tmp_path/"receipts"
    attest_first_day(clean,manifest,receipts,now=DUE)
    b=create_backup(clean,manifest,receipts,tmp_path/"backups",now=DUE)
    file=Path(b["snapshot_directory"])/"clean_shadow_90d.sqlite3"
    with file.open("ab") as out:
        out.write(b"altered")
    with pytest.raises(BackupError,match="SHA-256"):
        inspect_closeout(clean,manifest,receipts,tmp_path/"backups",now=DUE)


def test_first_day_live_receipt_mismatch_is_rejected(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    clean=_shadow(db,tmp_path)
    receipts=tmp_path/"receipts"
    attest_first_day(clean,manifest,receipts,now=DUE)
    receipt=next(receipts.glob("first-day-*.json"))
    doc=json.loads(receipt.read_text())
    doc["first_day_events_sha256"]="0"*64
    receipt.write_text(json.dumps(doc))
    with pytest.raises(AdmissionError, match="Live first-day evidence"):
        inspect_closeout(clean,manifest,receipts,tmp_path/"backups",now=DUE)


def test_backup_missing_after_due_never_passes(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    clean=_shadow(db,tmp_path)
    receipts=tmp_path/"receipts"
    attest_first_day(clean,manifest,receipts,now=DUE)
    result=inspect_closeout(
        clean,manifest,receipts,tmp_path/"not-yet",
        now=datetime(2026,10,23,2,40,tzinfo=timezone.utc),
    )
    assert result["status"]=="BLOCKED_POST_AUDIT_BACKUP_MISSING"

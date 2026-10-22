"""Offline clean evidence snapshots: never modify live frozen research DB."""
from __future__ import annotations
from datetime import datetime,timezone
from pathlib import Path
import sqlite3
import pytest

from test_clean_cohort import cohort, record, DUE  # noqa: F401
from quantlab.clean_backup import BackupError, create_backup, verify_backup
from quantlab.clean_first_day_attestation import attest_first_day


def _copy_to_frozen_name(db, tmp_path):
    src=sqlite3.connect(db)
    destination=tmp_path / "clean_shadow_90d.db"
    dst=sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return destination


def test_consistent_backup_and_restored_ledger_without_receipt(cohort,tmp_path):
    db,manifest=cohort
    clean=_copy_to_frozen_name(db,tmp_path)
    out=tmp_path/"mirror"
    before=clean.read_bytes()
    result=create_backup(clean,manifest,tmp_path/"missing",out,
                         now=datetime(2026,10,22,8,0,tzinfo=timezone.utc))
    assert result["status"]=="CONSISTENT_LOCAL_BACKUP_VERIFIED"
    assert result["record_counts"]=={
        "shadow_accounts":6,"shadow_events":0,"shadow_quote_evidence":0}
    assert not result["first_day_receipt_attached"]
    restored=verify_backup(result["snapshot_directory"],manifest)
    assert restored["status"]=="CONSISTENT_BACKUP_RESTORED_READONLY"
    assert not restored["offhost_independent_copy_proven"]
    assert clean.read_bytes()==before
    assert (out/"snapshot-20261022T080000Z"/"manifest.json").exists()


def test_first_day_receipt_and_ledger_replay_verify_on_restore(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    clean=_copy_to_frozen_name(db,tmp_path)
    receipt_dir=tmp_path/"receipts"
    attested=attest_first_day(clean,manifest,receipt_dir,now=DUE)
    assert attested["receipt_created"]
    backup=create_backup(clean,manifest,receipt_dir,tmp_path/"mirror",now=DUE)
    assert backup["first_day_receipt_attached"]
    assert backup["record_counts"]["shadow_events"]==6
    assert backup["record_counts"]["shadow_quote_evidence"]==2
    assert verify_backup(backup["snapshot_directory"],manifest)["first_day_receipt_present_and_matched"]


def test_post_backup_file_mutation_is_detected(cohort,tmp_path):
    db,manifest=cohort
    clean=_copy_to_frozen_name(db,tmp_path)
    result=create_backup(clean,manifest,tmp_path/"none",tmp_path/"mirror",
                         now=DUE)
    saved=Path(result["snapshot_directory"])/"clean_shadow_90d.sqlite3"
    with saved.open("ab") as fp:
        fp.write(b"post-backup tampering")
    with pytest.raises(BackupError,match="SHA-256 changed"):
        verify_backup(result["snapshot_directory"],manifest)


def test_new_backup_cannot_overwrite_existing_snapshot(cohort,tmp_path):
    db,manifest=cohort
    clean=_copy_to_frozen_name(db,tmp_path)
    one=create_backup(clean,manifest,tmp_path/"none",tmp_path/"mirror",now=DUE)
    assert Path(one["snapshot_directory"]).is_dir()
    with pytest.raises(FileExistsError):
        create_backup(clean,manifest,tmp_path/"none",tmp_path/"mirror",now=DUE)


def test_due_first_day_missing_receipt_keeps_backup_but_not_approval(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    clean=_copy_to_frozen_name(db,tmp_path)
    result=create_backup(clean,manifest,tmp_path/"no-receipt",tmp_path/"backup",now=DUE)
    assert result["status"]=="LOCAL_BACKUP_MISSING_DUE_FIRST_DAY_RECEIPT"
    assert result["record_counts"]["shadow_events"]==6
    assert result["record_counts"]["shadow_quote_evidence"]==2
    assert verify_backup(result["snapshot_directory"],manifest)[
        "first_day_receipt_present_and_matched"] is False

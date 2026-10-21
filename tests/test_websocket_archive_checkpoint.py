"""Witness a fully recovered WebSocket evidence chain, then cheap seal auditing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantlab.websocket_archive_checkpoint import (
    create_checkpoint,audit_checkpoint,main
)
from quantlab.websocket_incremental_archive import (
    add_segment,verify_chain,MANIFEST,PAYLOAD
)
from test_websocket_incremental_archive import seed,TODAY
from test_websocket_archive import add_session


def fixture(tmp_path,n=3):
    live=tmp_path/"source.sqlite"
    seed(live,n)
    root=tmp_path/"sealed"
    for _ in range(n):
        add_segment(live_db=live,root=root,through_date=TODAY,max_sessions=1)
    return live,root


def test_checkpoint_created_only_after_full_raw_restore(tmp_path):
    live,root=fixture(tmp_path,3)
    witness=create_checkpoint(root)
    assert witness["status"]=="FULL_RECOVERY_CHECKPOINT_CREATED"
    assert witness["frames"]==21 and witness["sessions"]==3
    assert witness["full_recovery_performed"]
    checkpoint=Path(witness["checkpoint"])
    payload=json.loads(checkpoint.read_text())
    assert payload["raw_frame_hashes_verified_at_creation"] is True
    assert not payload["independent_offsite_anchor"]
    assert payload["source_db"]==str(live.resolve())
    first=audit_checkpoint(root,max_unverified_segments=0)
    assert first["gzip_and_manifest_SHA256"]=="PASS"
    assert first["status"]=="LATEST_PREFIX_SEALED_NO_NEW_SEGMENTS"
    assert first["unverified_new_segments"]==0
    assert not first["full_recovery_performed_in_this_audit"]
    assert not first["checkpoint_is_signed_or_offsite"]


def test_incremental_suffix_marked_unverified_until_new_full_recovery(tmp_path):
    live,root=fixture(tmp_path,2)
    first=create_checkpoint(root)
    original=Path(first["checkpoint"]).read_bytes()
    add_session(live,number=2)
    add_segment(live_db=live,root=root,through_date=TODAY,max_sessions=1)
    status=audit_checkpoint(root,max_unverified_segments=0)
    assert status["unverified_new_segments"]==1
    assert status["status"]=="NEW_SEGMENTS_NEED_FULL_RECOVERY_CHECKPOINT"
    assert not status["within_unverified_suffix_limit"]
    assert audit_checkpoint(root,max_unverified_segments=1)["within_unverified_suffix_limit"]
    second=create_checkpoint(root)
    assert second["sessions"]==3
    assert audit_checkpoint(root,max_unverified_segments=0)["unverified_new_segments"]==0
    assert Path(first["checkpoint"]).read_bytes()==original
    assert first["checkpoint"]!=second["checkpoint"]
    assert create_checkpoint(root)["status"]=="EXISTING_FULL_CHECKPOINT_PRESERVED"


def test_altered_gzip_or_manifest_always_fail_fast_checkpoint_audit(tmp_path):
    live,root=fixture(tmp_path,2)
    create_checkpoint(root)
    gz=root/"seg-000001"/PAYLOAD
    original=gz.read_bytes()
    gz.write_bytes(original[:-4]+b"XXXX")
    with pytest.raises(ValueError,match="SHA|compressed"):
        audit_checkpoint(root)
    gz.write_bytes(original)
    manifest=root/"seg-000002"/MANIFEST
    old=manifest.read_bytes()
    modified=json.loads(old)
    modified["utc_date"]="2000-01-01"
    manifest.write_text(json.dumps(modified))
    with pytest.raises(ValueError,match="checkpoint|fingerprint|digest|SHA"):
        audit_checkpoint(root)


def test_checkpoint_filename_and_provenance_cannot_be_forged(tmp_path):
    live,root=fixture(tmp_path,1)
    checkpoint=create_checkpoint(root)["checkpoint"]
    p=Path(checkpoint)
    obj=json.loads(p.read_text())
    obj["independent_offsite_anchor"]=True
    p.write_text(json.dumps(obj))
    with pytest.raises(ValueError,match="Invalid checkpoint witness"):
        audit_checkpoint(root)
    obj["independent_offsite_anchor"]=False
    obj["raw_frame_hashes_verified_at_creation"]=False
    p.write_text(json.dumps(obj))
    with pytest.raises(ValueError,match="Invalid checkpoint witness"):
        audit_checkpoint(root)


def test_missing_checkpoint_is_not_a_full_restore_and_enforce_fails(tmp_path):
    live,root=fixture(tmp_path,2)
    r=audit_checkpoint(root)
    assert r["status"]=="NO_FULL_RECOVERY_CHECKPOINT"
    assert r["unverified_new_segments"]==2
    assert not r["checkpoint_creation_had_full_recovery"]
    assert main(["--root",str(root),"--max-unverified","0"])==2
    assert main(["--root",str(root)])==0


def test_invalid_limit_symlink_and_missing_segments_refused(tmp_path):
    live,root=fixture(tmp_path,1)
    with pytest.raises(ValueError,match="Invalid permitted"):
        audit_checkpoint(root,max_unverified_segments=-1)
    target=tmp_path/"dangerous"
    target.mkdir()
    (root/"checkpoints").symlink_to(target,target_is_directory=True)
    with pytest.raises(ValueError,match="Unsafe checkpoint"):
        create_checkpoint(root)
    with pytest.raises(ValueError,match="Unsafe checkpoint"):
        audit_checkpoint(root)

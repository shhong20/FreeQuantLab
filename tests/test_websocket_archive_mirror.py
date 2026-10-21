"""Non-destructive cross-volume public WS incremental mirror and restore tests."""
from __future__ import annotations

from pathlib import Path
import json

import pytest

from quantlab.websocket_archive_mirror import mirror
from quantlab.websocket_archive_seals import verify_seals
from quantlab.websocket_incremental_archive import add_segment,verify_chain,MANIFEST,PAYLOAD
from test_websocket_incremental_archive import seed,TODAY
from test_websocket_archive import add_session


def setup(tmp_path,n=4):
    live=tmp_path/"source.db"
    seed(live,n)
    source=tmp_path/"sealed"
    for _ in range(n):
        res=add_segment(live_db=live,root=source,through_date=TODAY,max_sessions=1)
        assert res["status"]=="NEW_INCREMENTAL_SEGMENT_SEALED_AND_RESTORED"
    return live,source,tmp_path/"other-volume"


def test_same_fs_refused_as_disaster_copy(tmp_path):
    live,src,dest=setup(tmp_path,2)
    with pytest.raises(ValueError,match="separate mounted volume"):
        mirror(source_root=src,dest_root=dest)
    assert not dest.exists()


def test_cross_mount_logic_dry_run_copy_restore_and_repeat_without_duplicate(tmp_path):
    live,src,dest=setup(tmp_path,3)
    first=mirror(source_root=src,dest_root=dest,max_segments=2,
                 require_other_mount=False)
    assert first["new_segments_copied"]==2
    assert first["missing_source_segments_to_copy"]==1
    assert first["status"]=="SIMULATED_MIRROR_RESTORED_NOT_INDEPENDENT"
    assert not first["offsite_disaster_recovery_proven"]
    assert first["mirror_recovery"]["frames"]==14
    follow=mirror(source_root=src,dest_root=dest,max_segments=2,
                  require_other_mount=False)
    assert follow["new_segments_copied"]==1
    assert follow["missing_source_segments_to_copy"]==0
    assert follow["mirror_recovery"]["frames"]==21
    rerun=mirror(source_root=src,dest_root=dest,
                 require_other_mount=False)
    assert rerun["new_segments_copied"]==0
    assert verify_seals(dest)["archived_frames"]==21
    assert verify_chain(dest)["frames"]==21
    assert len(list(dest.glob("seg-*")))==3


def test_source_advances_after_mirror_and_existing_prefix_unchanged(tmp_path):
    live,src,dest=setup(tmp_path,2)
    mirror(source_root=src,dest_root=dest,require_other_mount=False)
    original=(dest/"seg-000001"/MANIFEST).read_bytes()
    add_session(live,number=2)
    add_segment(live_db=live,root=src,through_date=TODAY,max_sessions=1)
    r=mirror(source_root=src,dest_root=dest,require_other_mount=False)
    assert r["new_segments_copied"]==1
    assert r["mirror_recovery"]["sessions"]==3
    assert (dest/"seg-000001"/MANIFEST).read_bytes()==original


def test_corrupt_existing_mirror_blocks_copy_and_recovery(tmp_path):
    live,src,dest=setup(tmp_path,2)
    mirror(source_root=src,dest_root=dest,require_other_mount=False)
    blob=dest/"seg-000001"/PAYLOAD
    content=bytearray(blob.read_bytes())
    content[-8]^=1
    blob.write_bytes(content)
    with pytest.raises(ValueError,match="SHA|compressed"):
        verify_seals(dest)
    with pytest.raises(ValueError,match="SHA|compressed"):
        mirror(source_root=src,dest_root=dest,require_other_mount=False)


def test_source_missing_middle_segment_and_divergent_manifest_refused(tmp_path):
    live,src,dest=setup(tmp_path,3)
    mirror(source_root=src,dest_root=dest,require_other_mount=False)
    (src/"seg-000002"/MANIFEST).unlink()
    with pytest.raises((ValueError,FileNotFoundError)):
        mirror(source_root=src,dest_root=dest,require_other_mount=False)


def test_mirror_symlink_and_nested_destination_protection(tmp_path):
    live,src,dest=setup(tmp_path,1)
    with pytest.raises(ValueError,match="outside"):
        mirror(source_root=src,dest_root=src/"nested",require_other_mount=False)
    dest.symlink_to(src,target_is_directory=True)
    with pytest.raises(ValueError,match="outside|Unsafe"):
        mirror(source_root=src,dest_root=dest,require_other_mount=False)


def test_manifest_seal_audit_not_misrepresented_as_raw_frame_restore(tmp_path):
    live,src,dest=setup(tmp_path,2)
    r=verify_seals(src)
    assert r["status"]=="MANIFEST_AND_COMPRESSED_HASHES_VERIFIED_NOT_RESTORED"
    assert r["no_sqlite_restore_performed"] is True
    assert r["inner_frame_sha_and_causal_links_not_revalidated"]
    with pytest.raises(ValueError,match="No incremental archive segments"):
        verify_seals(tmp_path/"empty")


def test_missing_and_bounded_inputs_fail_closed(tmp_path):
    live,src,dest=setup(tmp_path,1)
    with pytest.raises(ValueError,match="bounded"):
        mirror(source_root=src,dest_root=dest,max_segments=100,
               require_other_mount=False)
    with pytest.raises(FileNotFoundError):
        mirror(source_root=tmp_path/"no-source",dest_root=dest,
               require_other_mount=False)
    assert not dest.exists()


def test_volume_mirror_recovery_works_after_source_archive_unmounted(tmp_path):
    live,src,dest=setup(tmp_path,3)
    result=mirror(source_root=src,dest_root=dest,require_other_mount=False)
    assert result["mirror_recovery"]["frames"]==21
    # Simulate *source archive* loss without touching the live trade capture.
    # This proves recovered data reads only the mirror's gzip+manifest.
    src.rename(tmp_path/"source-unavailable")
    recovered=verify_chain(dest)
    assert recovered["sessions"]==3
    assert recovered["frames"]==21
    assert recovered["all_frame_hashes"]=="PASS"


def test_mirror_refuses_backup_target_with_divergent_preexisting_segment(tmp_path):
    live,src,dest=setup(tmp_path,2)
    mirror(source_root=src,dest_root=dest,require_other_mount=False)
    manifest=dest/"seg-000001"/MANIFEST
    data=json.loads(manifest.read_text())
    data["utc_date"]="2011-01-01"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError,match="prefix|hash chain|diverges"):
        mirror(source_root=src,dest_root=dest,require_other_mount=False)
    # Never silently correct or overwrite the tampered backup.
    assert json.loads(manifest.read_text())["utc_date"]=="2011-01-01"

def test_fast_source_checkpoint_still_requires_full_destination_reconstruction(tmp_path,monkeypatch):
    from quantlab.websocket_archive_checkpoint import create_checkpoint
    from quantlab import websocket_archive_mirror as mod
    live,src,dest=setup(tmp_path,3)
    with pytest.raises(ValueError,match="Missing or stale"):
        mirror(source_root=src,dest_root=dest,require_other_mount=False,
               use_verified_source_checkpoint=True)
    create_checkpoint(src)
    original=mod.verify_chain
    checks=[]
    def spy(path,**kwargs):
        checks.append(str(path))
        # Source no longer redundantly restored. Dest MUST restore.
        assert str(Path(path).resolve())!=str(src.resolve())
        return original(path,**kwargs)
    monkeypatch.setattr(mod,"verify_chain",spy)
    result=mirror(source_root=src,dest_root=dest,require_other_mount=False,
                  use_verified_source_checkpoint=True)
    assert result["source_full_restore_skipped_using_checkpoint"] is True
    assert result["destination_full_restore_performed"]
    assert result["mirror_recovery"]["frames"]==21
    assert checks and all(str(dest)==x for x in checks)


def test_fast_source_checkpoint_invalid_after_new_append(tmp_path):
    from quantlab.websocket_archive_checkpoint import create_checkpoint
    live,src,dest=setup(tmp_path,2)
    create_checkpoint(src)
    add_session(live,number=2)
    add_segment(live_db=live,root=src,through_date=TODAY,max_sessions=1)
    with pytest.raises(ValueError,match="Missing or stale"):
        mirror(source_root=src,dest_root=dest,require_other_mount=False,
               use_verified_source_checkpoint=True)
    # Full validation remains an independent safe fallback.
    normal=mirror(source_root=src,dest_root=dest,require_other_mount=False)
    assert normal["mirror_recovery"]["frames"]==21

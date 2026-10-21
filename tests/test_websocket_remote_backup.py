"""Transport-neutral off-host backup protocol, receiver-only SQLite recovery."""
from __future__ import annotations

from pathlib import Path
import json
import shutil

import pytest

from quantlab.websocket_remote_backup import (
    create_bundle,accept_bundle,receiver_status,BUNDLE_FILE
)
from quantlab.websocket_incremental_archive import add_segment,verify_chain,MANIFEST,PAYLOAD
from test_websocket_incremental_archive import seed,TODAY
from test_websocket_archive import add_session


def fixture(tmp_path,n=3):
    live=tmp_path/"capture.sqlite3"
    seed(live,n)
    source=tmp_path/"sealed"
    for _ in range(n):
        add_segment(live_db=live,root=source,through_date=TODAY,max_sessions=1)
    dest=tmp_path/"receiver"
    staging=tmp_path/"outbox"
    staging.mkdir()
    return live,source,dest,staging


def bundle(source,outbox,index,base=0,max_segments=12):
    return create_bundle(source_root=source,outbox=outbox/("batch-"+str(index)),
                         base_segment_count=base,max_segments=max_segments)


def test_three_segment_portable_export_and_independent_receive(tmp_path):
    live,src,dest,out=fixture(tmp_path,3)
    d=bundle(src,out,0,0)
    assert d["exported_segments"]==3
    assert d["source_full_restored_at_export"]
    assert not d["transport_performed"]
    r=accept_bundle(bundle_dir=d["bundle"],target_root=dest)
    assert r["newly_received_segments"]==3
    assert r["sessions"]==3 and r["frames"]==21
    assert r["prior_book_trade_links"]==9
    assert receiver_status(dest)["segment_count"]==3
    result=verify_chain(dest)
    assert result["all_frame_hashes"]=="PASS"
    src.rename(tmp_path/"archived-source-unavailable")
    # Recipient continues to restore independently without contact to source
    assert verify_chain(dest)["frames"]==21


def test_resume_only_new_segments_and_never_replace_existing(tmp_path):
    live,src,dest,out=fixture(tmp_path,3)
    first=bundle(src,out,1,0,max_segments=2)
    r=accept_bundle(bundle_dir=first["bundle"],target_root=dest)
    assert r["newly_received_segments"]==2 and r["frames"]==14
    snapshot=(dest/"seg-000001"/PAYLOAD).read_bytes()
    follow=bundle(src,out,2,2,max_segments=2)
    r=accept_bundle(bundle_dir=follow["bundle"],target_root=dest)
    assert r["newly_received_segments"]==1 and r["frames"]==21
    r=accept_bundle(bundle_dir=follow["bundle"],target_root=dest)
    assert r["newly_received_segments"]==0
    assert snapshot==(dest/"seg-000001"/PAYLOAD).read_bytes()


def test_transfer_truncation_hash_corruption_and_recovery(tmp_path):
    live,src,dest,out=fixture(tmp_path,2)
    d=bundle(src,out,1)
    f=Path(d["bundle"])/"seg-000001"/PAYLOAD
    original=f.read_bytes()
    f.write_bytes(original[:-30])
    with pytest.raises(ValueError,match="hash|SHA|mismatch"):
        accept_bundle(bundle_dir=d["bundle"],target_root=dest)
    assert not dest.exists()
    f.write_bytes(original)
    good=accept_bundle(bundle_dir=d["bundle"],target_root=dest)
    assert good["frames"]==14


def test_receiver_different_prefix_forbidden_no_automatic_repair(tmp_path):
    live,src,dest,out=fixture(tmp_path,3)
    a=bundle(src,out,1,0,1)
    accept_bundle(bundle_dir=a["bundle"],target_root=dest)
    b=bundle(src,out,2,1,2)
    payload=Path(b["bundle"])/BUNDLE_FILE
    doc=json.loads(payload.read_text())
    doc["base_manifest_sha256"]="0"*64
    payload.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match="prefix"):
        accept_bundle(bundle_dir=b["bundle"],target_root=dest)
    assert verify_chain(dest)["frames"]==7
    assert (dest/"seg-000001"/PAYLOAD).is_file()
    assert not (dest/"seg-000002").exists()


def test_malformed_new_segment_rolls_back_only_new_receiver_rows(tmp_path):
    live,src,dest,out=fixture(tmp_path,3)
    accept_bundle(bundle_dir=bundle(src,out,1,0,1)["bundle"],target_root=dest)
    d=bundle(src,out,2,1,2)
    # Inner frame tamper with valid outer gzip/manifest impossible unless
    # both recomputed. For deterministic rollback exercise, replace later
    # second segment's ordinal from #3 to invalid. Input validation refuses
    # it without modifying #1.
    f=Path(d["bundle"])/BUNDLE_FILE
    doc=json.loads(f.read_text())
    doc["segments"][-1]["name"]="seg-000005"
    f.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match="consecutive"):
        accept_bundle(bundle_dir=d["bundle"],target_root=dest)
    assert verify_chain(dest)["segments"]==1


def test_reject_symlink_bundle_and_unexpected_files(tmp_path):
    live,src,dest,out=fixture(tmp_path,1)
    d=bundle(src,out,1)
    path=Path(d["bundle"])
    (path/"evil-extra").write_text("junk")
    with pytest.raises(ValueError,match="Extra"):
        accept_bundle(bundle_dir=path,target_root=dest)
    (path/"evil-extra").unlink()
    (path/"seg-000001"/PAYLOAD).rename(path/"outside")
    (path/"seg-000001"/PAYLOAD).symlink_to(path/"outside")
    with pytest.raises(ValueError,match="Extra|Unsafe"):
        accept_bundle(bundle_dir=path,target_root=dest)


def test_transfer_refuses_nested_source_or_destination_and_invalid_bounds(tmp_path):
    live,src,dest,out=fixture(tmp_path,2)
    with pytest.raises(ValueError,match="outside"):
        create_bundle(source_root=src,outbox=src/"nested",base_segment_count=0)
    with pytest.raises(ValueError,match="Invalid target"):
        bundle(src,out,2,-1)
    with pytest.raises(ValueError,match="Invalid transfer"):
        bundle(src,out,3,0,max_segments=13)
    d=bundle(src,out,1)
    with pytest.raises(ValueError,match="isolated"):
        accept_bundle(bundle_dir=d["bundle"],target_root=Path(d["bundle"])/"dest")


def test_receiver_missing_parent_and_missing_source_fail_closed(tmp_path):
    live,src,dest,out=fixture(tmp_path,1)
    d=bundle(src,out,1)
    with pytest.raises(FileNotFoundError,match="pre-provisioned"):
        accept_bundle(bundle_dir=d["bundle"],target_root=tmp_path/"missing-parent"/"dest")
    with pytest.raises(FileNotFoundError):
        create_bundle(source_root=tmp_path/"missing-source",outbox=out/"none",base_segment_count=0)


def test_source_grows_after_bundle_only_earlier_archive_is_recovered(tmp_path):
    live,src,dest,out=fixture(tmp_path,1)
    first=bundle(src,out,1)
    add_session(live,number=1)
    add_segment(live_db=live,root=src,through_date=TODAY,max_sessions=1)
    a=accept_bundle(bundle_dir=first["bundle"],target_root=dest)
    assert a["frames"]==7 and a["sessions"]==1
    second=bundle(src,out,2,base=1)
    b=accept_bundle(bundle_dir=second["bundle"],target_root=dest)
    assert b["frames"]==14 and b["sessions"]==2

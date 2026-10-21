"""Read-only live -> WSL archive -> other-volume copied public WS backlog."""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3

import pytest

from quantlab.websocket_backup_gap import measure_gap,main
from quantlab.websocket_archive_mirror import mirror
from quantlab.websocket_incremental_archive import add_segment
from test_websocket_archive_mirror import setup
from test_websocket_archive import add_session
from test_websocket_incremental_archive import TODAY


NOW_MS=1792580000000


def report(live,src,dest):
    return measure_gap(live_db=live,archive_root=src,mirror_root=dest,now_ms=NOW_MS)


def test_all_archived_and_mirrored_counts_and_checkpoint_age(tmp_path):
    live,src,dest=setup(tmp_path,3)
    mirror(source_root=src,dest_root=dest,require_other_mount=False)
    result=report(live,src,dest)
    assert result["completed_sessions_in_live_db"]==3
    assert result["fully_sealed_sessions_in_WSL"]==3
    assert result["mirrored_completed_sessions_on_second_volume"]==3
    assert result["not_yet_mirrored_completed_sessions"]==0
    assert result["new_segments_not_mirrored"]==0
    assert result["source_and_mirror_compressed_SHA"]=="PASS"
    assert not result["SQLite_full_restore_in_this_check"]
    assert not result["independent_offsite_recovery_verified"]


def test_source_new_sessions_exposes_backup_gap_until_archive_mirrored(tmp_path):
    live,src,dest=setup(tmp_path,2)
    mirror(source_root=src,dest_root=dest,require_other_mount=False)
    add_session(live,number=2)
    x=report(live,src,dest)
    assert x["unarchived_completed_sessions"]==1
    assert x["not_yet_mirrored_completed_sessions"]==1
    assert x["new_segments_not_mirrored"]==0
    add_segment(live_db=live,root=src,through_date=TODAY,max_sessions=1)
    y=report(live,src,dest)
    assert y["unarchived_completed_sessions"]==0
    assert y["not_yet_mirrored_completed_sessions"]==1
    assert y["new_segments_not_mirrored"]==1
    mirror(source_root=src,dest_root=dest,require_other_mount=False)
    z=report(live,src,dest)
    assert z["not_yet_mirrored_completed_sessions"]==0
    assert z["new_segments_not_mirrored"]==0


def test_invalid_older_live_prefix_cannot_pass_backlog_audit(tmp_path):
    live,src,dest=setup(tmp_path,2)
    mirror(source_root=src,dest_root=dest,require_other_mount=False)
    with sqlite3.connect(live) as c:
        c.execute("UPDATE sessions SET started_ms=started_ms+5 WHERE id='fixture-1'")
    with pytest.raises(ValueError,match="align"):
        report(live,src,dest)


def test_damaged_mirrored_gzip_refuses_clean_report(tmp_path):
    live,src,dest=setup(tmp_path,2)
    mirror(source_root=src,dest_root=dest,require_other_mount=False)
    f=dest/"seg-000001"/"records.jsonl.gz"
    raw=bytearray(f.read_bytes());raw[-7]^=77;f.write_bytes(raw)
    with pytest.raises(ValueError,match="SHA"):
        report(live,src,dest)


def test_missing_mirror_and_bad_threshold_fail(tmp_path):
    live,src,dest=setup(tmp_path,1)
    with pytest.raises(ValueError,match="Mirror is absent"):
        report(live,src,dest)
    with pytest.raises(ValueError,match="Invalid backlog threshold"):
        main(["--max-unmirrored","-1"])

"""Incremental verified market data archives without source deletion."""
from __future__ import annotations

from datetime import datetime,timezone
from pathlib import Path
import gzip
import json
import sqlite3
import pytest

from quantlab.websocket_incremental_archive import (
    add_segment,verify_chain,PAYLOAD,MANIFEST,
)
from test_websocket_archive import add_session


TODAY="2026-10-21"


def seed(path,n=5):
    for i in range(n):add_session(path,number=i)


def test_resumable_incremental_chain_reconstructs_all_sessions_and_book_links(tmp_path):
    live=tmp_path/"source.sqlite"
    seed(live,5)
    root=tmp_path/"incremental"
    a=add_segment(live_db=live,root=root,through_date=TODAY,max_sessions=2)
    b=add_segment(live_db=live,root=root,through_date=TODAY,max_sessions=2)
    c=add_segment(live_db=live,root=root,through_date=TODAY,max_sessions=2)
    assert [x["new_sessions"] for x in (a,b,c)]==[2,2,1]
    assert a["new_frames"]==14 and b["new_frames"]==14
    assert c["total_archived_sessions"]==5
    assert (root/"seg-000001"/PAYLOAD).exists()
    assert (root/"seg-000003"/MANIFEST).exists()
    empty=add_segment(live_db=live,root=root,through_date=TODAY)
    assert empty["status"]=="NO_NEW_ELIGIBLE_FINALIZED_SESSIONS"
    assert len(list(root.glob("seg-*")))==3
    verified=verify_chain(root)
    assert verified["sessions"]==5 and verified["frames"]==35
    assert verified["prior_book_trade_links"]==15
    assert verified["all_frame_hashes"]=="PASS"
    assert verified["real_orders_enabled"] is False
    restored=tmp_path/"restored.db"
    recovery=verify_chain(root,restore_to=restored)
    assert recovery["frames"]==35
    with sqlite3.connect(restored) as con,sqlite3.connect(live) as original:
        assert con.execute("SELECT count(*) FROM frames").fetchone()==(35,)
        assert con.execute("SELECT raw_sha256,raw FROM frames ORDER BY seq").fetchall()==original.execute(
            "SELECT raw_sha256,raw FROM frames ORDER BY seq").fetchall()
        assert con.execute("SELECT prev_book_seq FROM frames ORDER BY seq").fetchall()==original.execute(
            "SELECT prev_book_seq FROM frames ORDER BY seq").fetchall()
    assert len(list(root.glob("seg-*")))==3


def test_no_current_day_by_default_until_the_next_utc_day(tmp_path):
    live=tmp_path/"live.db"
    seed(live,2)
    result=add_segment(live_db=live,root=tmp_path/"archive",max_sessions=2)
    assert result["status"]=="NO_NEW_ELIGIBLE_FINALIZED_SESSIONS"
    assert not list((tmp_path/"archive").glob("seg-*"))


def test_resume_after_source_receives_new_sessions_and_unchanged_original(tmp_path):
    live=tmp_path/"live.db"
    seed(live,2)
    root=tmp_path/"archive"
    first=add_segment(live_db=live,root=root,max_sessions=10,through_date=TODAY)
    prev=(root/"seg-000001"/MANIFEST).read_bytes()
    add_session(live,number=2)
    result=add_segment(live_db=live,root=root,max_sessions=10,through_date=TODAY)
    assert result["new_sessions"]==1
    assert (root/"seg-000001"/MANIFEST).read_bytes()==prev
    assert verify_chain(root)["sessions"]==3
    with sqlite3.connect(live) as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()==(3,)


def test_missing_first_segment_and_chain_manifest_rewrite_refused(tmp_path):
    live=tmp_path/"live.db";seed(live,4)
    root=tmp_path/"segments"
    for _ in range(2):
        add_segment(live_db=live,root=root,through_date=TODAY,max_sessions=2)
    first=root/"seg-000001"/MANIFEST
    data=json.loads(first.read_text())
    data["utc_date"]="2000-01-01"
    first.write_text(json.dumps(data))
    with pytest.raises(ValueError,match="hash chain"):
        verify_chain(root)
    with pytest.raises(ValueError,match="hash chain"):
        add_segment(live_db=live,root=root,through_date=TODAY)
    first.parent.rename(root/"seg-000005")
    with pytest.raises(ValueError,match="Missing/reordered"):
        verify_chain(root)


def test_corrupted_gzip_and_bogus_manifest_cannot_restore(tmp_path):
    live=tmp_path/"live.db";seed(live,2)
    root=tmp_path/"segments"
    add_segment(live_db=live,root=root,through_date=TODAY)
    gz=root/"seg-000001"/PAYLOAD
    raw=bytearray(gz.read_bytes())
    raw[-12]^=31
    gz.write_bytes(raw)
    with pytest.raises(ValueError,match="SHA"):
        verify_chain(root)
    assert not list(tmp_path.glob("*restore*"))


def test_incomplete_session_held_back_until_finalized(tmp_path):
    live=tmp_path/"live.db"
    add_session(live,number=0)
    add_session(live,number=1,unfinished=True)
    root=tmp_path/"segments"
    r=add_segment(live_db=live,root=root,through_date=TODAY,max_sessions=10)
    assert r["new_sessions"]==1
    assert verify_chain(root)["sessions"]==1
    with sqlite3.connect(live) as c:
        c.execute("""UPDATE sessions SET status='BOUNDED_CAPTURE_COMPLETE',
                    finished_ms=started_ms+30000,
                    frame_count=(SELECT count(*) FROM frames WHERE session_id='fixture-1')
                    WHERE id='fixture-1'""")
    r=add_segment(live_db=live,root=root,through_date=TODAY)
    assert r["new_sessions"]==1
    assert verify_chain(root)["sessions"]==2


def test_snapshot_rejects_cross_source_or_forged_source_count(tmp_path):
    a=tmp_path/"a.db";b=tmp_path/"b.db"
    seed(a,2);seed(b,2)
    root=tmp_path/"segments"
    add_segment(live_db=a,root=root,through_date=TODAY)
    with pytest.raises(ValueError,match="different live source"):
        add_segment(live_db=b,root=root,through_date=TODAY)


def test_strict_restore_disallows_live_source_and_archived_tree(tmp_path):
    live=tmp_path/"live.db";seed(live,1)
    root=tmp_path/"seg"
    add_segment(live_db=live,root=root,through_date=TODAY)
    with pytest.raises(ValueError,match="Never restore"):
        verify_chain(root,restore_to=live)
    with pytest.raises(ValueError,match="immutable archive"):
        verify_chain(root,restore_to=root/"bogus.sqlite")
    good=tmp_path/"outside.sqlite"
    good.write_text("do not overwrite")
    with pytest.raises(FileExistsError):
        verify_chain(root,restore_to=good)
    assert good.read_text()=="do not overwrite"


def test_live_writer_can_append_during_mvcc_selection_without_retroactive_inclusion(tmp_path,monkeypatch):
    import quantlab.websocket_incremental_archive as mod
    source=tmp_path/"live.sqlite"
    seed(source,2)
    original=mod._emit_rows
    def writer_during_snapshot(connection,rows,target):
        add_session(source,number=2)
        return original(connection,rows,target)
    monkeypatch.setattr(mod,"_emit_rows",writer_during_snapshot)
    root=tmp_path/"archive"
    one=add_segment(live_db=source,root=root,through_date=TODAY,max_sessions=10)
    assert one["new_sessions"]==2
    assert verify_chain(root)["sessions"]==2
    monkeypatch.setattr(mod,"_emit_rows",original)
    two=add_segment(live_db=source,root=root,through_date=TODAY,max_sessions=10)
    assert two["new_sessions"]==1
    assert verify_chain(root)["frames"]==21


def test_utc_day_scoped_segments_never_combine_two_dates(tmp_path):
    from datetime import datetime,timezone
    live=tmp_path/"live.db"
    seed(live,2)
    older=int(datetime(2026,10,20,23,50,tzinfo=timezone.utc).timestamp()*1000)
    nextday=int(datetime(2026,10,21,0,10,tzinfo=timezone.utc).timestamp()*1000)
    with sqlite3.connect(live) as con:
        con.execute("UPDATE sessions SET started_ms=?,finished_ms=? WHERE id='fixture-0'",
                    (older,older+30000))
        con.execute("UPDATE sessions SET started_ms=?,finished_ms=? WHERE id='fixture-1'",
                    (nextday,nextday+30000))
    root=tmp_path/"archive"
    a=add_segment(live_db=live,root=root,through_date="2026-10-20")
    assert a["new_sessions"]==1 and a["utc_date"]=="2026-10-20"
    b=add_segment(live_db=live,root=root,through_date=TODAY)
    assert b["new_sessions"]==1 and b["utc_date"]=="2026-10-21"
    assert verify_chain(root)["sessions"]==2


def test_mutated_older_operational_source_is_not_silently_treated_as_complete(tmp_path):
    live=tmp_path/"live.db"
    seed(live,3)
    root=tmp_path/"archive"
    add_segment(live_db=live,root=root,through_date=TODAY,max_sessions=2)
    with sqlite3.connect(live) as conn:
        # Tamper only with test DB to create a watermark hole.
        conn.execute("DELETE FROM frames WHERE session_id='fixture-0'")
        conn.execute("DELETE FROM sessions WHERE id='fixture-0'")
    with pytest.raises(ValueError,match="watermark"):
        add_segment(live_db=live,root=root,through_date=TODAY)


def test_no_future_day_and_empty_source_fail_closed(tmp_path):
    source=tmp_path/"live.db"
    add_session(source,number=0)
    with pytest.raises(ValueError,match="future"):
        add_segment(live_db=source,root=tmp_path/"out",through_date="2100-01-01")
    with pytest.raises(FileNotFoundError,match="missing"):
        add_segment(live_db=tmp_path/"missing.db",root=tmp_path/"out")

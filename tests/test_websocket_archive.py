"""Lossless SQLite WAL backup, gzip roundtrip, recovery and tamper regression."""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import pytest

from quantlab.continuous_portfolio import ASSETS
from quantlab.websocket_microstructure import (
    open_db,validate_frame,append_validated,PUBLIC_WSS,
)
from quantlab.websocket_archive import (
    _extract,_sha,create_archive,verify_archive,PAYLOAD_NAME,MANIFEST_NAME,
)
from test_websocket_microstructure import BASE,book,trade,encode


def add_session(path,number=0,*,unfinished=False):
    conn=open_db(path)
    sid=f"fixture-{number}"
    with conn:
        conn.execute(
            """INSERT INTO sessions
               (id,started_ms,finished_ms,status,intended_seconds,
                endpoint,mode,frame_count)
               VALUES(?,?,?, ?,30,?,'PUBLIC_READ_ONLY',0)""",
            (sid,BASE+number*60000,
             None if unfinished else BASE+number*60000+30000,
             "RUNNING" if unfinished else "BOUNDED_CAPTURE_COMPLETE",PUBLIC_WSS),
        )
        totals={"frames":0,"books":0,"trades":0,"duplicates":0,
                "causal_trade_links":0,"unlinked_trades":0}
        cache={}
        for ix,asset in enumerate(ASSETS):
            stamp=BASE+number*60000+1000+ix*100
            raw=encode(book(asset,stamp))
            bx=validate_frame(raw,stamp+100)
            append_validated(conn,sid,bx,cache,totals)
            if ix!=3:
                tx=validate_frame(
                    encode(trade(asset,stamp+220,id=9000+number*10+ix)),
                    stamp+330,
                )
                append_validated(conn,sid,tx,cache,totals)
        if not unfinished:
            conn.execute("UPDATE sessions SET frame_count=? WHERE id=?",
                         (totals["frames"],sid))
    conn.close()


def test_real_sqlite_online_backup_roundtrip_and_append_only(tmp_path):
    source=tmp_path/"live.sqlite3"
    add_session(source)
    before=_sha(source)
    root=tmp_path/"archives"
    first=create_archive(live_db=source,archive_root=root)
    assert first["status"]=="LOSSLESS_SQLITE_ROUNDTRIP_VERIFIED"
    assert first["sqlite_facts"]["sessions"]==1
    assert first["sqlite_facts"]["frames"]==7
    assert first["full_frame_audit"]["trade_to_prior_book_links"]==3
    assert first["compressed_bytes"]<first["restored_database_bytes"]
    assert first["source_evidence_deleted"] is False
    assert _sha(source)==before
    manifest=json.loads((Path(first["archive_path"])/MANIFEST_NAME).read_text())
    assert manifest["compression_level"]==6
    assert manifest["snapshot_may_contain_in_progress_session"] is False
    assert (Path(first["archive_path"])/PAYLOAD_NAME).exists()
    assert not list(root.glob(".staging*"))
    assert verify_archive(first["archive_path"])["sqlite_facts"]==first["sqlite_facts"]
    restored=tmp_path/"restored.sqlite3"
    evidence=verify_archive(first["archive_path"],restore_to=restored)
    assert restored.exists() and evidence["full_frame_audit"]==first["full_frame_audit"]
    with sqlite3.connect(restored) as c:
        assert c.execute("SELECT count(*) FROM frames").fetchone()[0]==7
        assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    with pytest.raises(FileExistsError):
        verify_archive(first["archive_path"],restore_to=restored)
    second=create_archive(live_db=source,archive_root=root)
    assert second["archive_path"]!=first["archive_path"]
    assert len(list(root.glob("ws-full-*")))==2


def test_wal_consistent_snapshot_not_copied_raw_and_live_writer_can_advance(tmp_path,monkeypatch):
    import quantlab.websocket_archive as module
    source=tmp_path/"live.sqlite3"
    add_session(source)
    original=module._snapshot
    def inject_writer_after_snapshot(src,dst):
        original(src,dst)
        add_session(source,number=1)
    monkeypatch.setattr(module,"_snapshot",inject_writer_after_snapshot)
    evidence=create_archive(live_db=source,archive_root=tmp_path/"archives")
    assert evidence["sqlite_facts"]["sessions"]==1
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT count(*) FROM sessions").fetchone()[0]==2
        assert c.execute("SELECT count(*) FROM frames").fetchone()[0]==14
    assert evidence["full_frame_audit"]["sessions"]==1


def test_incomplete_live_session_refused_without_published_archive(tmp_path):
    source=tmp_path/"live.db"
    add_session(source,unfinished=True)
    root=tmp_path/"archives"
    with pytest.raises(ValueError,match="in-progress"):
        create_archive(live_db=source,archive_root=root)
    assert not list(root.glob("ws-full-*"))
    assert not list(root.glob(".staging-*"))
    assert source.exists()


def test_unauthorized_archive_compressed_bytes_are_detected(tmp_path):
    source=tmp_path/"live.db"
    add_session(source)
    a=create_archive(live_db=source,archive_root=tmp_path/"archives")
    f=Path(a["archive_path"])/PAYLOAD_NAME
    raw=bytearray(f.read_bytes())
    raw[-10]^=0x20
    f.write_bytes(raw)
    with pytest.raises(ValueError,match="SHA256"):
        verify_archive(a["archive_path"])


def test_forged_manifest_metadata_and_payload_file_are_refused(tmp_path):
    source=tmp_path/"live.db"
    add_session(source)
    a=create_archive(live_db=source,archive_root=tmp_path/"archives")
    manifest=Path(a["archive_path"])/MANIFEST_NAME
    doc=json.loads(manifest.read_text())
    doc["real_orders_enabled"]=True
    manifest.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match="unsafe archive"):
        verify_archive(a["archive_path"])
    doc["real_orders_enabled"]=False
    doc["payload_file"]="../live.db"
    manifest.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match="unsafe archive"):
        verify_archive(a["archive_path"])


def test_restore_bound_and_invalid_dest_does_not_overwrite(tmp_path):
    source=tmp_path/"live.db"
    add_session(source)
    a=create_archive(live_db=source,archive_root=tmp_path/"archives")
    archive=Path(a["archive_path"])/PAYLOAD_NAME
    snapshot_hash=json.loads((Path(a["archive_path"])/MANIFEST_NAME).read_text())["snapshot_sha256"]
    d=tmp_path/"bad.db"
    with pytest.raises(ValueError,match="exceeds"):
        _extract(archive,d,expected_size=200,expected_sha256=snapshot_hash)
    assert not d.exists()
    with pytest.raises(ValueError,match="separate"):
        create_archive(live_db=source,archive_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        create_archive(live_db=tmp_path/"missing.sqlite",archive_root=tmp_path/"r")


def test_archived_frame_corruption_rejected_even_if_sqlite_and_gzip_digests_updated(tmp_path):
    import gzip
    from quantlab.websocket_archive import _sha
    source=tmp_path/"live.db"
    add_session(source)
    result=create_archive(live_db=source,archive_root=tmp_path/"archives")
    archive_dir=Path(result["archive_path"])
    compressed=archive_dir/PAYLOAD_NAME
    sqlite_file=tmp_path/"corrupt.sqlite3"
    with gzip.open(compressed,"rb") as src,sqlite_file.open("wb") as dst:
        import shutil
        shutil.copyfileobj(src,dst)
    with sqlite3.connect(sqlite_file) as c:
        c.execute("UPDATE frames SET raw=? WHERE kind='trade'",(b'tampered',))
    # Commit the deliberately corrupt *test copy* from SQLite WAL into
    # the image before rebuilding the malicious .gz. Not a live DB change.
    with sqlite3.connect(sqlite_file) as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with compressed.open("wb") as dst:
        # No real compaction of the live source: rewrite only archive COPY.
        with gzip.GzipFile(fileobj=dst,mode="wb",mtime=0) as gz:
            gz.write(sqlite_file.read_bytes())
    manifest_file=archive_dir/MANIFEST_NAME
    manifest=json.loads(manifest_file.read_text())
    manifest["snapshot_sha256"]=_sha(sqlite_file)
    manifest["snapshot_bytes"]=sqlite_file.stat().st_size
    manifest["gzip_sha256"]=_sha(compressed)
    manifest["gzip_bytes"]=compressed.stat().st_size
    manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match="Tampered archived raw"):
        verify_archive(archive_dir)


def test_online_backup_includes_committed_uncheckpointed_wal_rows(tmp_path):
    source=tmp_path/"producer.sqlite3"
    add_session(source)
    # Pin old reader snapshot to force later committed data to stay in WAL.
    reader=open_db(source)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM sessions").fetchone()
    add_session(source,number=1)
    wal=Path(str(source)+"-wal")
    assert wal.exists() and wal.stat().st_size>0
    try:
        evidence=create_archive(live_db=source,archive_root=tmp_path/"archives")
        assert evidence["sqlite_facts"]["sessions"]==2
        assert evidence["sqlite_facts"]["frames"]==14
        assert evidence["full_frame_audit"]["trade_to_prior_book_links"]==6
        assert reader.execute("SELECT count(*) FROM sessions").fetchone()[0]==1
    finally:
        reader.rollback();reader.close()


def test_archive_restore_must_not_target_live_database_even_if_missing(tmp_path):
    source=tmp_path/"source.sqlite3"
    add_session(source)
    summary=create_archive(live_db=source,archive_root=tmp_path/"archives")
    with pytest.raises(ValueError,match="Never restore directly into"):
        verify_archive(summary["archive_path"],restore_to=source)
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT count(*) FROM sessions").fetchone()[0]==1


def test_archive_payload_symlink_is_not_followed(tmp_path):
    source=tmp_path/"source.sqlite3"
    add_session(source)
    summary=create_archive(live_db=source,archive_root=tmp_path/"archives")
    payload=Path(summary["archive_path"])/PAYLOAD_NAME
    elsewhere=tmp_path/"other.gz"
    payload.rename(elsewhere)
    payload.symlink_to(elsewhere)
    with pytest.raises(ValueError,match="redirected"):
        verify_archive(summary["archive_path"])

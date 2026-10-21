"""Lossless, append-only and NON-DESTRUCTIVE archive of the public WS SQLite ledger.

Uses SQLite's online backup API (rather than copying the WAL-backed .db file),
then deterministically gzip-compresses the consistent backup. Only archives
that pass full raw frame SHA / causal-link audits are published. Always
round-trip decompress and verify before publication.

This is full-DB snapshot preservation, NOT incremental storage compaction;
no retention deletion, no live WAL checkpoint, no stopping WebSocket timers,
no order placement or new model calibration. Run manually until disk budget
and rollover/partition strategy are independently validated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from .websocket_microstructure import validate_frame, MAX_BOOK_TRADE_LAG_MS

ARCHIVE_SCHEMA=1
PAYLOAD_NAME="snapshot.sqlite3.gz"
MANIFEST_NAME="manifest.json"
BUFFER_SIZE=1024*1024
# Guard unexpected decompression expansion, still permits years of snapshots.
MAX_RESTORE_BYTES=40*1024**3


def _sha(path:Path)->str:
    digest=sha256()
    with path.open("rb") as stream:
        while data:=stream.read(BUFFER_SIZE):
            digest.update(data)
    return digest.hexdigest()


def _sqlite_stats(path:Path)->dict:
    with sqlite3.connect("file:"+str(path.resolve())+"?mode=ro",uri=True,timeout=15) as c:
        if c.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise ValueError("Archive SQLite integrity check failed")
        if c.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Archive has broken foreign key references")
        sessions=c.execute("SELECT count(*),min(started_ms),max(started_ms) FROM sessions").fetchone()
        frames=c.execute(
            """SELECT count(*),
                      sum(CASE WHEN kind='orderbook' THEN 1 ELSE 0 END),
                      sum(CASE WHEN kind='trade' THEN 1 ELSE 0 END),
                      sum(length(raw)),
                      max(seq)
               FROM frames"""
        ).fetchone()
        pending=c.execute("SELECT count(*) FROM sessions WHERE status='RUNNING'").fetchone()[0]
    # The active capture may overlap the backup. Store consistent evidence
    # including RUNNING sessions rather than claiming everything finalized.
    return {
        "sessions":sessions[0],"first_session_started_ms":sessions[1],
        "last_session_started_ms":sessions[2],
        "frames":frames[0],"orderbook_frames":frames[1] or 0,
        "public_trade_frames":frames[2] or 0,
        "raw_frame_bytes":frames[3] or 0,"latest_frame_seq":frames[4],
        "running_sessions_at_snapshot":pending,
    }


def _snapshot(source:Path,output:Path)->None:
    # A raw cp(source.sqlite3) is UNSAFE with WAL. Online backup creates a
    # self-consistent SQLite DB without mutating the live producer.
    with sqlite3.connect(
        "file:"+str(source.resolve())+"?mode=ro",uri=True,timeout=20
    ) as src:
        with sqlite3.connect(str(output),timeout=20) as dst:
            src.backup(dst,pages=256,sleep=.05)
    os.chmod(output,0o600)


def _compress(source:Path,target:Path)->None:
    with source.open("rb") as src,target.open("xb") as raw:
        os.chmod(target,0o600)
        with gzip.GzipFile(fileobj=raw,mode="wb",filename="",mtime=0,
                           compresslevel=6) as encoded:
            shutil.copyfileobj(src,encoded,length=BUFFER_SIZE)
        raw.flush()
        os.fsync(raw.fileno())


def _extract(
    archive:Path,dest:Path, *,
    expected_size:int,expected_sha256:str,
    max_size:int=MAX_RESTORE_BYTES,
)->dict:
    if (type(expected_size) is not int or not 0<expected_size<=max_size
        or len(expected_sha256)!=64):
        raise ValueError("Invalid archive decompression bounds")
    if dest.exists():
        raise FileExistsError("Refuse overwriting existing restored database")
    digest=sha256();size=0
    try:
        with archive.open("rb") as incoming,gzip.GzipFile(fileobj=incoming,mode="rb") as compressed, dest.open("xb") as output:
            os.chmod(dest,0o600)
            while chunk:=compressed.read(BUFFER_SIZE):
                size+=len(chunk)
                if size>expected_size or size>max_size:
                    raise ValueError("Archive decompression exceeds claimed bound")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size!=expected_size or digest.hexdigest()!=expected_sha256:
            raise ValueError("Restored SQLite bytes do not match archive digest")
        return {"uncompressed_bytes":size,"uncompressed_sha256":digest.hexdigest()}
    except Exception:
        if dest.exists():dest.unlink()
        raise


def _proof(path:Path)->dict:
    # Verify EVERY archived frame, without materializing years of raw JSON
    # into a Python list. SQLite streams rows; indexed references are looked
    # up by seq. This is independent of the 2h live watchdog's bounded audit.
    with sqlite3.connect(
        "file:"+str(path.resolve())+"?mode=ro",uri=True,timeout=15
    ) as con:
        sessions=list(con.execute(
            "SELECT id,status,frame_count FROM sessions"
        ))
        # A backup taken during a live 30-second WS session is self-consistent,
        # but its unfinished frame count is not a valid finalized receipt.
        if any(status=="RUNNING" for _,status,_ in sessions):
            raise ValueError("Snapshot contains in-progress session; retry between captures")
        frame_counts={sid:0 for sid,_,_ in sessions}
        books=trades=links=0
        for (seq,sid,rx,ts,kind,market,stream,tid,ref,lag,label,h,raw) in con.execute(
            """SELECT seq,session_id,receipt_ms,exchange_ms,kind,market,
                   stream_type,trade_id,prev_book_seq,asof_exchange_lag_ms,
                   link_status,raw_sha256,raw FROM frames ORDER BY seq"""
        ):
            if sha256(raw).hexdigest()!=h:
                raise ValueError("Tampered archived raw WebSocket SHA256")
            parsed=validate_frame(raw,rx)
            if (parsed["kind"],parsed["market"],parsed["exchange_ms"],
                parsed["stream_type"],parsed["trade_id"])!=(kind,market,ts,stream,tid):
                raise ValueError("Archived frame metadata mismatch")
            if sid not in frame_counts:
                raise ValueError("Archived frame belongs to unknown session")
            frame_counts[sid]+=1
            if kind=="orderbook":
                books+=1
                if ref is not None or label!="BOOK" or lag is not None:
                    raise ValueError("Invalid book causal metadata")
            else:
                trades+=1
                if ref is None:
                    if label!="NO_CAUSAL_BOOK" or lag is not None:
                        raise ValueError("Invalid unlinked trade metadata")
                else:
                    earlier=con.execute(
                        "SELECT seq,session_id,kind,market,receipt_ms,exchange_ms "
                        "FROM frames WHERE seq=?",(ref,)
                    ).fetchone()
                    if (earlier is None or earlier[0]>=seq or earlier[1]!=sid
                        or earlier[2]!="orderbook" or earlier[3]!=market
                        or earlier[4]>rx or not 0<=ts-earlier[5]<=MAX_BOOK_TRADE_LAG_MS
                        or lag!=ts-earlier[5]
                        or label!="CAUSAL_VISIBLE_L2_REFERENCE_ONLY"):
                        raise ValueError("Archived trade has invalid prior-book link")
                    links+=1
        if any(frame_counts[sid]!=claimed for sid,_,claimed in sessions):
            raise ValueError("Archived session frame count mismatch")
        duplicates=con.execute(
            """SELECT 1 FROM frames WHERE kind='trade'
               GROUP BY market,trade_id HAVING count(*)>1 LIMIT 1"""
        ).fetchone()
        if duplicates:
            raise ValueError("Archived repeated public trade IDs")
    return {
        "sessions":len(sessions),
        "complete_sessions":sum(status=="BOUNDED_CAPTURE_COMPLETE"
                                for _,status,_ in sessions),
        "degraded_or_failed_sessions":sum(status!="BOUNDED_CAPTURE_COMPLETE"
                                          for _,status,_ in sessions),
        "book_frames":books,
        "market_trade_frames":trades,
        "trade_to_prior_book_links":links,
        "raw_sha256_integrity":"PASS",
    }


def _fsync_directory(path:Path)->None:
    # Make atomic staging-to-final rename durable across sudden host power loss.
    fd=os.open(path,os.O_RDONLY | getattr(os,"O_DIRECTORY",0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_manifest(path:Path,payload:dict)->None:
    with path.open("x",encoding="utf-8") as f:
        json.dump(payload,f,ensure_ascii=False,indent=2,sort_keys=True)
        f.write("\n")
        f.flush();os.fsync(f.fileno())
    os.chmod(path,0o600)


def _manifest(path:Path)->dict:
    record=json.loads((path/MANIFEST_NAME).read_text("utf-8"))
    if (record.get("schema_version")!=ARCHIVE_SCHEMA or
        record.get("kind")!="PUBLIC_WS_SQLITE_ONLINE_BACKUP_GZIP" or
        record.get("payload_file")!=PAYLOAD_NAME or
        record.get("real_orders_enabled") is not False or
        record.get("live_source_never_modified") is not True):
        raise ValueError("Unrecognized or unsafe archive provenance")
    size=record.get("snapshot_bytes")
    if type(size) is not int or size<=0 or size>MAX_RESTORE_BYTES:
        raise ValueError("Invalid snapshot size in manifest")
    for field in ("snapshot_sha256","gzip_sha256"):
        val=record.get(field)
        if not isinstance(val,str) or len(val)!=64 or any(x not in "0123456789abcdef" for x in val):
            raise ValueError("Invalid archive digest format")
    if type(record.get("gzip_bytes")) is not int or record["gzip_bytes"]<=0:
        raise ValueError("Invalid compressed byte count")
    return record


def verify_archive(path:str|Path,*,restore_to:str|Path|None=None)->dict:
    root=Path(path)
    manifest=_manifest(root)
    source=root/PAYLOAD_NAME
    if source.is_symlink() or not source.is_file() or source.stat().st_size!=manifest["gzip_bytes"]:
        raise ValueError("Compressed archive missing, redirected, or size altered")
    if _sha(source)!=manifest["gzip_sha256"]:
        raise ValueError("Compressed archive SHA256 mismatch")
    if restore_to is None:
        with tempfile.TemporaryDirectory(prefix="fql-archive-verify-") as folder:
            target=Path(folder)/"restored.sqlite3"
            unpacked=_extract(
                source,target,expected_size=manifest["snapshot_bytes"],
                expected_sha256=manifest["snapshot_sha256"],
            )
            facts=_sqlite_stats(target)
            proof=_proof(target)
    else:
        target=Path(restore_to)
        if target.resolve()==Path(manifest["source_db"]).resolve():
            raise ValueError("Never restore directly into the live producer database")
        if not target.parent.is_dir():
            raise FileNotFoundError("Restore destination parent directory absent")
        unpacked=_extract(
            source,target,expected_size=manifest["snapshot_bytes"],
            expected_sha256=manifest["snapshot_sha256"],
        )
        try:
            facts=_sqlite_stats(target)
            proof=_proof(target)
        except Exception:
            # Invalid new file is not retained as an acceptable recovery.
            target.unlink(missing_ok=True)
            raise
    if facts!=manifest["sqlite_facts"] or proof!=manifest["full_frame_audit"]:
        if restore_to is not None:
            Path(restore_to).unlink(missing_ok=True)
        raise ValueError("Restored frame facts differ from archive manifest")
    return {
        "status":"LOSSLESS_SQLITE_ROUNDTRIP_VERIFIED",
        "archive_path":str(root),"compressed_bytes":manifest["gzip_bytes"],
        "restored_database_bytes":unpacked["uncompressed_bytes"],
        "compressed_fraction":manifest["gzip_bytes"]/unpacked["uncompressed_bytes"],
        "sqlite_facts":facts,"full_frame_audit":proof,
        "live_db_modified":False,"source_evidence_deleted":False,
        "customer_fills_verified":False,"real_orders_enabled":False,
    }


def create_archive(*,live_db:str|Path,archive_root:str|Path)->dict:
    source=Path(live_db).resolve()
    if not source.is_file():
        raise FileNotFoundError("Public WebSocket source SQLite absent")
    root=Path(archive_root).resolve()
    if root==source.parent or root==source or source.is_relative_to(root):
        raise ValueError("Archive destination must be separate from live database")
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Never overwrite an earlier report. UUID prevents same-clock collisions.
    name=datetime.now(timezone.utc).strftime("ws-full-%Y%m%dT%H%M%SZ-")+uuid4().hex[:12]
    output=root/name
    working=Path(tempfile.mkdtemp(prefix=".staging-ws-",dir=root))
    os.chmod(working,0o700)
    snapshot=working/"snapshot.sqlite3"
    try:
        _snapshot(source,snapshot)
        facts=_sqlite_stats(snapshot)
        frame_proof=_proof(snapshot)
        initial_size=snapshot.stat().st_size
        digest=_sha(snapshot)
        compressed=working/PAYLOAD_NAME
        _compress(snapshot,compressed)
        snapshot.unlink()
        for leftover in working.glob("snapshot.sqlite3-*"):
            leftover.unlink()
        manifest={
            "schema_version":ARCHIVE_SCHEMA,
            "kind":"PUBLIC_WS_SQLITE_ONLINE_BACKUP_GZIP",
            "source_db":str(source),
            "snapshot_created_utc":datetime.now(timezone.utc).isoformat(),
            "payload_file":PAYLOAD_NAME,
            "snapshot_sha256":digest,"snapshot_bytes":initial_size,
            "gzip_sha256":_sha(compressed),"gzip_bytes":compressed.stat().st_size,
            "sqlite_facts":facts,"full_frame_audit":frame_proof,
            "snapshot_may_contain_in_progress_session":bool(facts["running_sessions_at_snapshot"]),
            "wal_consistent_sqlite_online_backup":True,
            "compression_level":6,"format":"gzip",
            "live_source_never_modified":True,
            "source_evidence_deleted":False,
            "incremental_archive":False,
            "repeated_full_snapshots_consume_additional_disk":True,
            "real_orders_enabled":False,
        }
        _write_manifest(working/MANIFEST_NAME,manifest)
        # Full round-trip from compressed bytes, verify all frame SHA and
        # causal links BEFORE making a finalized archive discoverable.
        verify_archive(working)
        _fsync_directory(working)
        if output.exists():
            raise FileExistsError("Append-only archive ID collision")
        os.rename(working,output)
        _fsync_directory(root)
        return verify_archive(output)
    finally:
        if working.exists():shutil.rmtree(working)


def main(argv=None)->int:
    ap=argparse.ArgumentParser(description="Lossless public WS SQLite archive (never deletes source)")
    ap.add_argument("--live-db",default="/home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3")
    ap.add_argument("--archive-root",default="/home/hong/apps/FreeQuantLab/state/microstructure/archives")
    ap.add_argument("--verify",default=None)
    ap.add_argument("--restore-to",default=None)
    args=ap.parse_args(argv)
    if args.restore_to is not None and args.verify is None:
        raise ValueError("Restoring requires an existing --verify archive")
    if args.verify:
        result=verify_archive(args.verify,restore_to=args.restore_to)
    else:
        result=create_archive(live_db=args.live_db,archive_root=args.archive_root)
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

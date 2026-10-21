"""Append-only date-scoped incremental public WebSocket evidence archives.

No source DB backup, DELETE, WAL checkpoint, customer orders or retention.
Each segment contains only *new*, finalized sessions and their frames, with
original sequence IDs, session metadata, frame bytes, SHA and causal refs.
Prior segments are cryptographically chained (NOT digitally signed).
All compressed segments can reconstruct a verified independent SQLite DB.

Explicit manual invocation only; default time cutoff is *yesterday UTC*.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from .websocket_microstructure import open_db
from .websocket_archive import _fsync_directory, _proof, _sha, _sqlite_stats

SCHEMA=1
SESSION_COLUMNS=("id","started_ms","finished_ms","status","intended_seconds",
                 "frame_count","rejected_frames","duplicated_trade_ids",
                 "reconnects","notes","endpoint","mode")
FRAME_COLUMNS=("seq","session_id","receipt_ms","exchange_ms","kind","market",
               "stream_type","trade_id","prev_book_seq","asof_exchange_lag_ms",
               "link_status","raw_sha256","raw")
PAYLOAD="records.jsonl.gz"
MANIFEST="manifest.json"
MAX_SEGMENT_SESSIONS=100
MAX_LINE=500_000
MAX_SEGMENT_PLAINTEXT=2*1024**3


def _json(value)->bytes:
    return (json.dumps(value,separators=(",",":"),ensure_ascii=False,
                       sort_keys=True)+"\n").encode()


def _key(row)->list:
    return [row[1],row[0]]


def _utc_day(ms)->str:
    return datetime.fromtimestamp(ms/1000,timezone.utc).date().isoformat()


def _segments(root:Path)->list[Path]:
    if not root.is_dir():
        return []
    entries=sorted(p for p in root.iterdir() if p.name.startswith("seg-"))
    for n,p in enumerate(entries,1):
        if p.name!=f"seg-{n:06d}" or not p.is_dir() or p.is_symlink():
            raise ValueError("Missing/reordered/unsafe incremental archive segment")
    return entries


def _read_manifest(p:Path)->dict:
    file=p/MANIFEST
    if file.is_symlink():
        raise ValueError("Unsafe manifest symlink")
    content=file.read_bytes()
    record=json.loads(content)
    if record.get("schema_version")!=SCHEMA or record.get("kind")!="PUBLIC_WS_INCREMENTAL":
        raise ValueError("Unsupported incremental evidence manifest")
    if record.get("payload")!=PAYLOAD or record.get("orders_enabled") is not False:
        raise ValueError("Unsafe incremental segment provenance")
    if record.get("seq")!=int(p.name[-6:]):
        raise ValueError("Segment ordinal mismatch")
    for field in ("payload_sha256","plaintext_sha256"):
        h=record.get(field)
        if not isinstance(h,str) or len(h)!=64 or any(c not in "0123456789abcdef" for c in h):
            raise ValueError("Invalid segment SHA256")
    if not isinstance(record.get("sessions"),int) or record["sessions"]<=0:
        raise ValueError("Empty or invalid archive segment")
    return record


def _manifest_hash(p:Path)->str:
    return _sha(p/MANIFEST)


def _read_chain(root:Path)->tuple[list[Path],list[dict]]:
    paths=_segments(root)
    previous=None
    last=None
    docs=[]
    source=None
    for path in paths:
        d=_read_manifest(path)
        if d["prior_manifest_sha256"]!=previous:
            raise ValueError("Broken incremental manifest hash chain")
        if source is not None and d["source_db"]!=source:
            raise ValueError("Inconsistent incremental evidence source")
        if last is not None and d["first_session_key"]<=last:
            raise ValueError("Duplicated/reordered incremental session boundary")
        if d["first_session_key"]>d["last_session_key"]:
            raise ValueError("Reversed segment boundaries")
        if d["source_session_count_through_last"]!=sum(
            x["sessions"] for x in docs)+d["sessions"]:
            raise ValueError("Cumulative archive count mismatch")
        previous=_manifest_hash(path)
        last=d["last_session_key"]
        source=d["source_db"]
        docs.append(d)
    return paths,docs


def _emit_rows(con:sqlite3.Connection,rows:list,output:Path)->dict:
    stats={"sessions":0,"frames":0,"books":0,"trades":0,
           "raw_bytes":0,"first_frame_seq":None,"last_frame_seq":None}
    digest=sha256();total=0
    def write_line(out,obj):
        nonlocal total
        data=_json(obj)
        total+=len(data)
        if total>MAX_SEGMENT_PLAINTEXT:
            raise ValueError("Archive segment too large: reduce max sessions")
        digest.update(data)
        out.write(data)
    ids=[r[0] for r in rows]
    placeholders=",".join("?" for _ in ids)
    with output.open("xb") as file:
        os.chmod(output,0o600)
        with gzip.GzipFile(fileobj=file,mode="wb",filename="",mtime=0,compresslevel=6) as gz:
            for r in rows:
                write_line(gz,{"type":"session","values":list(r)})
                stats["sessions"]+=1
            query=("SELECT "+",".join(FRAME_COLUMNS)+" FROM frames WHERE session_id IN ("+
                   placeholders+") ORDER BY seq")
            for frame in con.execute(query,ids):
                raw=frame[-1]
                if not isinstance(raw,bytes):
                    raise ValueError("Missing original frame bytes")
                values=list(frame)
                values[-1]=base64.b64encode(raw).decode("ascii")
                write_line(gz,{"type":"frame","values":values})
                seq=frame[0]
                if stats["first_frame_seq"] is None:
                    stats["first_frame_seq"]=seq
                stats["last_frame_seq"]=seq
                stats["frames"]+=1
                stats["books"]+=frame[4]=="orderbook"
                stats["trades"]+=frame[4]=="trade"
                stats["raw_bytes"]+=len(raw)
        file.flush()
        os.fsync(file.fileno())
    stats["plaintext_bytes"]=total
    stats["plaintext_sha256"]=digest.hexdigest()
    stats["gzip_bytes"]=output.stat().st_size
    stats["payload_sha256"]=_sha(output)
    return stats


def _stream_rows(path:Path,d:dict):
    f=path/PAYLOAD
    if f.is_symlink() or not f.is_file() or f.stat().st_size!=d["gzip_bytes"]:
        raise ValueError("Missing/truncated/symlink incremental gzip")
    if _sha(f)!=d["payload_sha256"]:
        raise ValueError("Incremental compressed SHA mismatch")
    size=0
    digest=sha256()
    with gzip.open(f,"rb") as source:
        while line:=source.readline(MAX_LINE+1):
            if len(line)>MAX_LINE:
                raise ValueError("Incremental decompression line limit")
            size+=len(line)
            if size>d["plaintext_bytes"] or size>MAX_SEGMENT_PLAINTEXT:
                raise ValueError("Incremental decompression size limit")
            digest.update(line)
            yield json.loads(line)
    if size!=d["plaintext_bytes"] or digest.hexdigest()!=d["plaintext_sha256"]:
        raise ValueError("Incremental uncompressed SHA/size mismatch")


def _load_verified(paths:list[Path],docs:list[dict],destination:Path)->dict:
    if destination.exists():
        raise FileExistsError("Refuse overwriting recovered WebSocket evidence")
    if not destination.parent.is_dir():
        raise FileNotFoundError("Recovery destination directory absent")
    try:
        con=open_db(destination)
        with con:
            seen_key=None
            last_frame=0
            for folder,d in zip(paths,docs):
                sessions=frames=books=trades=raw_bytes=0
                first_key=last_key=None
                first_seq=last_seq=None
                phase="sessions"
                for obj in _stream_rows(folder,d):
                    kind=obj.get("type")
                    values=obj.get("values")
                    if kind=="session":
                        if phase!="sessions" or not isinstance(values,list) or len(values)!=len(SESSION_COLUMNS):
                            raise ValueError("Invalid segment session order")
                        key=_key(values)
                        if seen_key is not None and key<=seen_key:
                            raise ValueError("Duplicate/out-of-order archived session")
                        seen_key=key
                        if _utc_day(values[1])!=d["utc_date"]:
                            raise ValueError("Incremental UTC date mismatch")
                        if values[3]=="RUNNING" or values[2] is None:
                            raise ValueError("Incomplete session in sealed increment")
                        con.execute(
                            "INSERT INTO sessions("+",".join(SESSION_COLUMNS)+") VALUES("+
                            ",".join("?" for _ in SESSION_COLUMNS)+")",values)
                        first_key=key if first_key is None else first_key
                        last_key=key
                        sessions+=1
                    elif kind=="frame":
                        phase="frames"
                        if not isinstance(values,list) or len(values)!=len(FRAME_COLUMNS):
                            raise ValueError("Invalid archived frame")
                        seq=values[0]
                        if type(seq) is not int or seq<=last_frame:
                            raise ValueError("Duplicated/nonmonotone archived frame seq")
                        last_frame=seq
                        if first_seq is None:first_seq=seq
                        last_seq=seq
                        try:raw=base64.b64decode(values[-1],validate=True)
                        except (ValueError,Exception) as e:
                            raise ValueError("Invalid archived frame base64") from e
                        if sha256(raw).hexdigest()!=values[-2]:
                            raise ValueError("Raw frame SHA mismatch in incremental archive")
                        values[-1]=raw
                        con.execute("INSERT INTO frames("+",".join(FRAME_COLUMNS)+") VALUES("+
                                    ",".join("?" for _ in FRAME_COLUMNS)+")",values)
                        frames+=1
                        books+=values[4]=="orderbook"
                        trades+=values[4]=="trade"
                        raw_bytes+=len(raw)
                    else:
                        raise ValueError("Unknown incremental record type")
                expected={"sessions":sessions,"frames":frames,
                          "books":books,"trades":trades,"raw_bytes":raw_bytes,
                          "first_frame_seq":first_seq,"last_frame_seq":last_seq}
                if (d["first_session_key"]!=first_key or
                    d["last_session_key"]!=last_key or
                    any(d[k]!=v for k,v in expected.items())):
                    raise ValueError("Segment records diverge from manifest")
        con.close()
        proof=_proof(destination)
        facts=_sqlite_stats(destination)
        if (proof["sessions"]!=sum(x["sessions"] for x in docs)
            or facts["frames"]!=sum(x["frames"] for x in docs)
            or proof["raw_sha256_integrity"]!="PASS"):
            raise ValueError("Archive chain full SQLite frame proof mismatch")
        return {"status":"INCREMENTAL_CHAIN_ROUNDTRIP_VERIFIED",
                "segments":len(docs),"sessions":proof["sessions"],
                "frames":facts["frames"],"books":proof["book_frames"],
                "trades":proof["market_trade_frames"],
                "prior_book_trade_links":proof["trade_to_prior_book_links"],
                "latest_session_key":docs[-1]["last_session_key"] if docs else None,
                "all_frame_hashes":"PASS","original_db_deleted":False,
                "actual_customer_fills":0,"real_orders_enabled":False}
    except Exception:
        for suffix in ("","-wal","-shm"):
            Path(str(destination)+suffix).unlink(missing_ok=True)
        raise


def verify_chain(root:str|Path,*,restore_to:str|Path|None=None)->dict:
    folder=Path(root).resolve()
    paths,docs=_read_chain(folder)
    if not docs:
        raise ValueError("No incremental archive segments")
    source=Path(docs[0]["source_db"]).resolve()
    if restore_to is not None:
        dest=Path(restore_to).resolve()
        if dest==source:
            raise ValueError("Never restore onto the operational source DB")
        if folder==dest or folder in dest.parents:
            raise ValueError("Restore must not be inside the immutable archive tree")
        return _load_verified(paths,docs,dest)
    with tempfile.TemporaryDirectory(prefix="fql-incremental-roundtrip-") as staging:
        result=_load_verified(paths,docs,Path(staging)/"restored.sqlite3")
    result["archive_root"]=str(folder)
    result["compressed_total_bytes"]=sum(x["gzip_bytes"] for x in docs)
    result["uncompressed_jsonl_total_bytes"]=sum(x["plaintext_bytes"] for x in docs)
    return result


def add_segment(*,live_db:str|Path,root:str|Path,through_date:str|None=None,
                max_sessions:int=20)->dict:
    source=Path(live_db).resolve()
    if not source.is_file():
        raise FileNotFoundError("Live public WS SQLite DB missing")
    if type(max_sessions) is not int or not 1<=max_sessions<=MAX_SEGMENT_SESSIONS:
        raise ValueError("Bounded max sessions 1..100 required")
    today=datetime.now(timezone.utc).date()
    limit=date.fromisoformat(through_date) if through_date else today-timedelta(days=1)
    if limit>today:
        raise ValueError("Cannot archive sessions dated in the future")
    archive_root=Path(root).resolve()
    if archive_root==source or archive_root==source.parent or source in archive_root.parents:
        raise ValueError("Invalid incremental archive destination")
    archive_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Same independent capture machine; reject accidental concurrent archivists.
    import fcntl
    lock=archive_root/".archive.lock"
    with lock.open("a+b") as lockfile:
        fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
        paths,docs=_read_chain(archive_root)
        if docs:
            # Fail closed on previously changed/dropped segments before resume.
            verify_chain(archive_root)
            if Path(docs[0]["source_db"]).resolve()!=source:
                raise ValueError("Incremental chain points to different live source")
        previous=docs[-1]["last_session_key"] if docs else None
        prev_hash=_manifest_hash(paths[-1]) if paths else None
        count=sum(d["sessions"] for d in docs)
        with sqlite3.connect("file:"+str(source)+"?mode=ro",uri=True,timeout=15) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN") # stable WAL MVCC view while live collector writes
            cursor=conn.execute(
                "SELECT "+",".join(SESSION_COLUMNS)+" FROM sessions "+
                "ORDER BY started_ms,id"
            )
            selected=[]
            seen_prior=0
            chosen_day=None
            for row in cursor:
                key=_key(row)
                if previous is not None and key<=previous:
                    seen_prior+=1
                    continue
                day=_utc_day(row[1])
                if day>limit.isoformat():break
                if row[3]=="RUNNING" or row[2] is None:break
                if chosen_day is not None and day!=chosen_day:break
                chosen_day=day
                selected.append(row)
                if len(selected)>=max_sessions:break
            if count!=seen_prior:
                raise ValueError("Archived session watermark no longer matches source")
            if not selected:
                return {"status":"NO_NEW_ELIGIBLE_FINALIZED_SESSIONS",
                        "archived_segments":len(docs),"sessions_already_archived":count,
                        "through_utc_date":limit.isoformat(),
                        "source_untouched":True}
            stage=Path(tempfile.mkdtemp(prefix=".stage-",dir=archive_root))
            os.chmod(stage,0o700)
            try:
                stats=_emit_rows(conn,selected,stage/PAYLOAD)
                first,last=_key(selected[0]),_key(selected[-1])
                manifest={
                    "schema_version":SCHEMA,"kind":"PUBLIC_WS_INCREMENTAL",
                    "seq":len(paths)+1,"source_db":str(source),
                    "utc_date":chosen_day,"created_utc":datetime.now(timezone.utc).isoformat(),
                    "first_session_key":first,"last_session_key":last,
                    "prior_manifest_sha256":prev_hash,
                    "source_session_count_through_last":count+len(selected),
                    "payload":PAYLOAD,"orders_enabled":False,
                    "source_rows_never_deleted":True,"no_automatic_retention":True,
                    **stats,
                }
                (stage/MANIFEST).write_bytes(_json(manifest))
                os.chmod(stage/MANIFEST,0o600)
                for file in (stage/MANIFEST,):
                    with file.open("rb") as fh:os.fsync(fh.fileno())
                _fsync_directory(stage)
                # Independently verify a full reconstituted DB before publish,
                # without rewriting any prior immutable archive.
                temp_root=Path(tempfile.mkdtemp(prefix=".verify-",dir=archive_root))
                try:
                    newdir=temp_root/f"seg-{len(paths)+1:06d}"
                    shutil.copytree(stage,newdir)
                    _load_verified(paths+[newdir],docs+[manifest],
                                   temp_root/"reconstructed.sqlite3")
                finally:shutil.rmtree(temp_root)
                final=archive_root/f"seg-{len(paths)+1:06d}"
                if final.exists():raise FileExistsError("Incremental segment exists")
                os.rename(stage,final)
                _fsync_directory(archive_root)
            finally:
                if stage.exists():shutil.rmtree(stage)
        return {"status":"NEW_INCREMENTAL_SEGMENT_SEALED_AND_RESTORED",
                "segment":str(final),"utc_date":chosen_day,
                "new_sessions":len(selected),"new_frames":stats["frames"],
                "compressed_bytes":stats["gzip_bytes"],
                "uncompressed_bytes":stats["plaintext_bytes"],
                "compression_ratio":stats["gzip_bytes"]/stats["plaintext_bytes"],
                "total_archived_sessions":count+len(selected),
                "source_untouched":True,"real_orders_enabled":False}


def main(argv=None):
    parser=argparse.ArgumentParser(description="Manual incremental public websocket archives, never delete original")
    parser.add_argument("--live-db",default="/home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3")
    parser.add_argument("--root",default="/home/hong/apps/FreeQuantLab/state/microstructure/incremental")
    parser.add_argument("--through-date",default=None,
                        help="UTC YYYY-MM-DD; default yesterday UTC; today is a manual research exception")
    parser.add_argument("--max-sessions",type=int,default=20)
    parser.add_argument("--verify",action="store_true")
    parser.add_argument("--restore-to",default=None)
    args=parser.parse_args(argv)
    if args.verify:
        result=verify_chain(args.root,restore_to=args.restore_to)
    else:
        if args.restore_to:raise ValueError("--restore-to requires --verify")
        result=add_segment(live_db=args.live_db,root=args.root,
                           through_date=args.through_date,max_sessions=args.max_sessions)
    print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

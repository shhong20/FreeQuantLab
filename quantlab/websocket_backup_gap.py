"""Read-only RPO / backlog report for public WebSocket incremental archives.

Separate original live SQLite capture, WSL sealed increment chain and D:
mirror. Quantify evidence not yet archived/mirrored; never treat filesystem
copies as offsite. Unlike a checkpoint seal audit, this checks live session
watermark, counts and archive/mirror prefix identities, but does not restore
all raw frames (call verify_chain separately for that).
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import time

from .websocket_archive import _sha
from .websocket_archive_seals import verify_seals
from .websocket_incremental_archive import _read_chain,MANIFEST


def measure_gap(
    *,live_db:str|Path,archive_root:str|Path,mirror_root:str|Path,
    now_ms:int|None=None,
)->dict:
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    if type(now_ms) is not int or now_ms<1_500_000_000_000:
        raise ValueError("Invalid current timestamp")
    live=Path(live_db).resolve()
    local=Path(archive_root).resolve()
    remote_volume=Path(mirror_root).resolve()
    if not live.is_file():
        raise FileNotFoundError("Live public WebSocket SQLite missing")
    original_paths,original_docs=_read_chain(local)
    if not original_docs:
        raise ValueError("No verified WSL archive chain")
    if Path(original_docs[0]["source_db"]).resolve()!=live:
        raise ValueError("Archive provenance differs from live source")
    verify_seals(local)
    copied_paths,copied_docs=_read_chain(remote_volume)
    if not copied_docs:
        raise ValueError("Mirror is absent; cannot attest backup availability")
    verify_seals(remote_volume)
    if len(copied_docs)>len(original_docs):
        raise ValueError("Mirror contains unexplained extra segments")
    for n in range(len(copied_docs)):
        if _sha(copied_paths[n]/MANIFEST)!=_sha(original_paths[n]/MANIFEST):
            raise ValueError("Mirror prefix diverged from WSL source")

    # Only request the two archive watermarks and the newest completed row;
    # never load the entire multi-year sessions table into Python RAM.
    with sqlite3.connect("file:"+str(live)+"?mode=ro",uri=True,timeout=15) as con:
        condition="status!='RUNNING' AND finished_ms IS NOT NULL"
        total=con.execute(
            "SELECT count(*) FROM sessions WHERE "+condition
        ).fetchone()[0]
        local_sessions=sum(x["sessions"] for x in original_docs)
        mirror_sessions=sum(x["sessions"] for x in copied_docs)
        if local_sessions>total or mirror_sessions>total:
            raise ValueError("Archived sessions exceed finalized live source")
        def watermark(n):
            return con.execute(
                "SELECT started_ms,id,finished_ms FROM sessions WHERE "+condition+
                " ORDER BY started_ms,id LIMIT 1 OFFSET ?",(n-1,)
            ).fetchone()
        local_row=watermark(local_sessions)
        mirror_row=watermark(mirror_sessions)
        newest=con.execute(
            "SELECT finished_ms FROM sessions WHERE "+condition+
            " ORDER BY started_ms DESC,id DESC LIMIT 1"
        ).fetchone()
    if list(local_row[:2])!=original_docs[-1]["last_session_key"]:
        raise ValueError("Source archive does not align with live session prefix")
    if list(mirror_row[:2])!=copied_docs[-1]["last_session_key"]:
        raise ValueError("Mirror does not align with live session prefix")
    last_archived_end=local_row[2]
    last_mirror_end=mirror_row[2]
    newest_end=newest[0] if newest else None
    unarchived=total-local_sessions
    pending=total-mirror_sessions
    return {
        "status":"PUBLIC_WS_READ_ONLY_BACKUP_LAG_ASSESSMENT",
        "checked_at_utc":datetime.fromtimestamp(now_ms/1000,timezone.utc).isoformat(),
        "completed_sessions_in_live_db":total,
        "fully_sealed_sessions_in_WSL":local_sessions,
        "mirrored_completed_sessions_on_second_volume":mirror_sessions,
        "unarchived_completed_sessions":unarchived,
        "not_yet_mirrored_completed_sessions":pending,
        "new_segments_not_mirrored":len(original_docs)-len(copied_docs),
        "latest_source_completed_ms":newest_end,
        "latest_archived_completed_ms":last_archived_end,
        "latest_mirrored_completed_ms":last_mirror_end,
        "mirror_recovery_point_age_seconds":max(0,(now_ms-last_mirror_end)/1000),
        "mirror_lag_versus_latest_completed_seconds":
            max(0,(newest_end-last_mirror_end)/1000) if newest_end else 0,
        "unmirrored_raw_frame_count_exact":False,
        "archived_prefix_session_rows_individually_revalidated":False,
        "last_archived_session_watermark_matches_live":True,
        "source_and_mirror_compressed_SHA":"PASS",
        "SQLite_full_restore_in_this_check":False,
        "both_volumes_same_machine":True,
        "independent_offsite_recovery_verified":False,
        "raw_sources_or_archives_deleted":False,
        "real_orders_enabled":False,
    }


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="Read-only live/WSL/D backup gap, no timer/actions")
    parser.add_argument("--live-db",default="/home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3")
    parser.add_argument("--archive",default="/home/hong/apps/FreeQuantLab/state/microstructure/incremental")
    parser.add_argument("--mirror",default="/mnt/d/FreeQuantLab-Backups/websocket-incremental")
    parser.add_argument("--max-unmirrored",type=int,default=None)
    args=parser.parse_args(argv)
    if args.max_unmirrored is not None and not 0<=args.max_unmirrored<=100000:
        raise ValueError("Invalid backlog threshold")
    result=measure_gap(live_db=args.live_db,archive_root=args.archive,
                       mirror_root=args.mirror)
    print(json.dumps(result,ensure_ascii=False))
    return (2 if args.max_unmirrored is not None
            and result["not_yet_mirrored_completed_sessions"]>args.max_unmirrored
            else 0)


if __name__=="__main__":
    raise SystemExit(main())

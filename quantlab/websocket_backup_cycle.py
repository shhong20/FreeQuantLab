"""Bounded, restart-safe, append-only public WebSocket archive backup cycle.

On every explicitly authorized run: measure source/mirror gap, archive only
finalized UTC sessions (today may be partial), independently restore source
and the D-volume mirror, then re-measure backup lag. No full raw-candle gap
fabrication, no deletion, no orders, no offsite-backup claims.

The independent service is fail-closed on low disk, missing separate mount,
chain size budget, concurrent invocation, incomplete mirror or source
provenance change. Defaults are a read-only preflight; --run is required.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from .websocket_archive_checkpoint import create_checkpoint
from .websocket_archive_mirror import mirror
from .websocket_backup_gap import measure_gap
from .websocket_incremental_archive import add_segment, _read_chain

DEFAULT_DB="/home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3"
DEFAULT_SOURCE="/home/hong/apps/FreeQuantLab/state/microstructure/incremental"
DEFAULT_DEST="/mnt/d/FreeQuantLab-Backups/websocket-incremental"
DEFAULT_REPORTS="/home/hong/apps/FreeQuantLab/state/microstructure/backup-receipts"
MAX_BATCHES=6
MAX_SESSIONS=20
MAX_ARCHIVE_BYTES=256*1024*1024
MIN_FREE_BYTES=2*1024*1024*1024


def _preflight(live:Path,source:Path,dest:Path,*,min_free_bytes:int,
               max_archive_bytes:int)->dict:
    if not live.is_file() or not source.is_dir() or not dest.is_dir():
        raise FileNotFoundError("Live, source archive, and mirror must already exist")
    if any(p.is_symlink() for p in (live,source,dest)):
        raise ValueError("Live or archive symlink prohibited")
    if source.stat().st_dev==dest.stat().st_dev:
        raise ValueError("Mirror is not on another mounted volume")
    for folder in (source,dest):
        if shutil.disk_usage(folder).free<min_free_bytes:
            raise OSError("Backup filesystem low-free-space guard failed")
    paths,docs=_read_chain(source)
    if not docs:
        raise ValueError("No verified source archive to resume")
    total=sum(int(x["gzip_bytes"]) for x in docs)
    if total>max_archive_bytes:
        raise ValueError("Source compressed chain exceeds bounded auto-run limit")
    if dest.resolve()==source.resolve() or source in dest.parents or dest in source.parents:
        raise ValueError("Source and mirror directories overlap")
    return {"source_segments":len(paths),
            "source_compressed_bytes":total,
            "separate_filesystem_verified":True,
            "offsite_hardware_independence_verified":False}


def run_backup_cycle(*,live_db:str|Path=DEFAULT_DB,
                     archive_root:str|Path=DEFAULT_SOURCE,
                     mirror_root:str|Path=DEFAULT_DEST,
                     report_root:str|Path=DEFAULT_REPORTS,
                     max_batches:int=MAX_BATCHES,
                     max_sessions:int=MAX_SESSIONS,
                     max_archive_bytes:int=MAX_ARCHIVE_BYTES,
                     min_free_bytes:int=MIN_FREE_BYTES,
                     perform:bool=False)->dict:
    if (type(max_batches) is not int or not 1<=max_batches<=MAX_BATCHES
        or type(max_sessions) is not int or not 1<=max_sessions<=MAX_SESSIONS
        or type(max_archive_bytes) is not int or max_archive_bytes<=0
        or type(min_free_bytes) is not int or min_free_bytes<0):
        raise ValueError("Invalid bounded archive runtime limits")
    live=Path(live_db).resolve()
    source=Path(archive_root).resolve()
    dest=Path(mirror_root).resolve()
    initial=_preflight(live,source,dest,min_free_bytes=min_free_bytes,
                       max_archive_bytes=max_archive_bytes)
    if not perform:
        gap=measure_gap(live_db=live,archive_root=source,mirror_root=dest)
        return {"status":"PREFLIGHT_ONLY_NO_ARCHIVE_OR_MIRROR_WRITES",
                "preflight":initial,"current_gap":gap,
                "automated_storage_deletion_enabled":False,
                "offsite_disaster_recovery_proven":False,
                "real_orders_enabled":False}
    reports=Path(report_root).resolve()
    if reports==source or reports in source.parents or reports==dest or reports in dest.parents:
        raise ValueError("Backup reports may not be written to evidence chain")
    if not reports.parent.is_dir() or reports.is_symlink():
        raise FileNotFoundError("Dedicated backup report parent missing or unsafe")
    reports.mkdir(exist_ok=True,mode=0o700)
    lock=reports/".backup-cycle.lock"
    with lock.open("a+b") as lockfile:
        fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Recheck after acquiring lock to fail closed if another actor
        # moved/modified the destination during a pending invocation.
        _preflight(live,source,dest,min_free_bytes=min_free_bytes,
                   max_archive_bytes=max_archive_bytes)
        new_sessions=new_frames=segments_created=0
        batches=0
        # Today UTC is an explicit partial-date exception; always only
        # finalized sessions from a consistent source MVCC read snapshot.
        through=datetime.now(timezone.utc).date().isoformat()
        for _ in range(max_batches):
            _preflight(live,source,dest,min_free_bytes=min_free_bytes,
                       max_archive_bytes=max_archive_bytes)
            result=add_segment(live_db=live,root=source,through_date=through,
                               max_sessions=max_sessions)
            if result["status"]=="NO_NEW_ELIGIBLE_FINALIZED_SESSIONS":
                break
            if result["status"]!="NEW_INCREMENTAL_SEGMENT_SEALED_AND_RESTORED":
                raise ValueError("Unexpected source seal result")
            new_sessions+=result["new_sessions"]
            new_frames+=result["new_frames"]
            segments_created+=1
            batches+=1
        # A fresh full SQLite recovery checkpoint validates ALL source
        # frames before choosing the accelerated MIRROR path.
        source_checkpoint=create_checkpoint(source)
        remote_copy=mirror(source_root=source,dest_root=dest,
                           max_segments=12,
                           use_verified_source_checkpoint=True)
        # Full mirror recovery ran and verified each raw SHA and FK.
        if (remote_copy["missing_source_segments_to_copy"]!=0 or
            not remote_copy["destination_full_restore_performed"] or
            remote_copy["status"]!="SEPARATE_MOUNT_MIRROR_RESTORED_AND_VERIFIED"):
            raise ValueError("Backup mirror not completely copied and recovered")
        target_checkpoint=create_checkpoint(dest)
        gap=measure_gap(live_db=live,archive_root=source,mirror_root=dest)
        # New sessions could finish DURING the verification, so report
        # measured residual lag. Never claim RPO==0 without rechecking.
        receipt={
            "status":("MIRROR_RECOVERED_NO_BACKLOG_AT_CHECK"
                      if gap["not_yet_mirrored_completed_sessions"]==0 else
                      "MIRROR_RECOVERED_WITH_NEW_SESSIONS_AFTER_SNAPSHOT"),
            "captured_at_utc":datetime.now(timezone.utc).isoformat(),
            "utc_archive_cutoff_is_partial_date":through,
            "new_sessions_sealed":new_sessions,"new_frames_sealed":new_frames,
            "new_source_segments":segments_created,"batches_used":batches,
            "final_source_checkpoint_status":source_checkpoint["status"],
            "final_mirror_checkpoint_status":target_checkpoint["status"],
            "mirror_total_segments":remote_copy["mirrored_segments"],
            "mirror_total_frames_restored":remote_copy["mirror_recovery"]["frames"],
            "mirror_original_raw_SHA256":"PASS",
            "gap_at_receipt":gap,
            "source_and_mirror_both_on_one_computer":True,
            "offsite_disaster_recovery_proven":False,
            "automated_storage_deletion_enabled":False,
            "automatic_model_promotion_enabled":False,
            "real_orders_enabled":False,
        }
        # Never reuse or overwrite the success evidence from a past run.
        from uuid import uuid4
        filename=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:10]+".json"
        fd,name=tempfile.mkstemp(prefix=".stage-",dir=reports)
        os.close(fd)
        staged=Path(name)
        try:
            os.chmod(staged,0o600)
            with staged.open("wb") as out:
                out.write((json.dumps(receipt,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode())
                out.flush();os.fsync(out.fileno())
            published=reports/filename
            os.rename(staged,published)
            receipt["receipt_path"]=str(published)
        finally:
            staged.unlink(missing_ok=True)
        return receipt


def main(argv=None)->int:
    ap=argparse.ArgumentParser(description="Bounded read-only by default WSL->D public WS backup")
    ap.add_argument("--run",action="store_true",help="Explicitly enable append-only archive/mirror")
    ap.add_argument("--live-db",default=DEFAULT_DB)
    ap.add_argument("--archive-root",default=DEFAULT_SOURCE)
    ap.add_argument("--mirror-root",default=DEFAULT_DEST)
    ap.add_argument("--report-root",default=DEFAULT_REPORTS)
    ap.add_argument("--max-batches",type=int,default=MAX_BATCHES)
    ap.add_argument("--max-sessions",type=int,default=MAX_SESSIONS)
    args=ap.parse_args(argv)
    try:
        result=run_backup_cycle(live_db=args.live_db,archive_root=args.archive_root,
                                mirror_root=args.mirror_root,report_root=args.report_root,
                                max_batches=args.max_batches,max_sessions=args.max_sessions,
                                perform=args.run)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if (not args.run or result["status"]=="MIRROR_RECOVERED_NO_BACKLOG_AT_CHECK") else 2
    except (OSError,ValueError,FileNotFoundError) as exc:
        print(json.dumps({"status":"BACKUP_FAILED_CLOSED",
                          "reason_type":type(exc).__name__,
                          "offsite_disaster_recovery_proven":False,
                          "source_deleted":False,"real_orders_enabled":False}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

"""Manual, lossless mirrored copies of sealed incremental PUBLIC WebSocket data.

Resumable prefix-only append on an independently mounted volume (e.g. D:).
Copy every raw compressed payload+manifest byte-for-byte; never rewrite
existing destination segments or touch the live collector DB.

Cross-volume means different mounted filesystems, NOT verified physical
independence or off-site disaster resilience. No background scheduler.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile

from .websocket_archive import _sha
from .websocket_archive_seals import verify_seals
from .websocket_incremental_archive import _read_chain, verify_chain, PAYLOAD, MANIFEST

MAX_COPY_SEGMENTS=12


def _safe_fsync_dir(path:Path)->bool:
    try:
        fd=os.open(path,os.O_RDONLY | getattr(os,"O_DIRECTORY",0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except OSError as exc:
        # Windows drvfs often rejects fsync on the directory itself. File
        # fsync still matters, but crash-durable rename CANNOT be attested.
        if exc.errno not in (errno.EINVAL,errno.EBADF,errno.ENOTSUP,errno.EOPNOTSUPP,
                             errno.EACCES,errno.EPERM):
            raise
        return False


def _copied_exact(src:Path,dst:Path)->None:
    if src.is_symlink() or not src.is_file():
        raise ValueError("Unsafe source evidence path")
    if dst.exists() or dst.is_symlink():
        raise FileExistsError("Refuse overwriting mirror evidence")
    with src.open("rb") as original,dst.open("xb") as written:
        os.chmod(dst,0o600)
        shutil.copyfileobj(original,written,length=1024*1024)
        written.flush()
        os.fsync(written.fileno())
    if src.stat().st_size!=dst.stat().st_size or _sha(src)!=_sha(dst):
        raise ValueError("Mirror copy SHA mismatch")


def _validate_paths(source:Path,dest:Path,*,require_other_mount:bool)->None:
    if source==dest or source in dest.parents or dest in source.parents:
        raise ValueError("Mirror destination must be outside source chain tree")
    if not source.is_dir():
        raise FileNotFoundError("Source archive chain missing")
    if dest.exists() and (not dest.is_dir() or dest.is_symlink()):
        raise ValueError("Unsafe mirror destination")
    if dest.parent.exists() and dest.parent.is_symlink():
        raise ValueError("Unsafe mirror destination parent")
    # st_dev check does not guarantee physically separate hardware, only
    # independent filesystem mount. Do not falsely label this offsite.
    if require_other_mount and source.stat().st_dev==dest.parent.stat().st_dev:
        raise ValueError("Not a separate mounted volume")


def mirror(
    *, source_root:str|Path,dest_root:str|Path,
    max_segments:int=MAX_COPY_SEGMENTS,require_other_mount:bool=True,
    use_verified_source_checkpoint:bool=False,
)->dict:
    if type(max_segments) is not int or not 1<=max_segments<=MAX_COPY_SEGMENTS:
        raise ValueError("Invalid bounded mirror batch size")
    source=Path(source_root).resolve()
    dest=Path(dest_root).absolute()
    _validate_paths(source,dest,require_other_mount=require_other_mount)
    # The default is full source reconstruction. The opt-in checkpoint path
    # only skips it if that precise source prefix was ALREADY reconstructed
    # and today's compressed manifest/hash chain still matches the witness.
    # The DESTINATION is always fully rebuilt even on the optimized path.
    if use_verified_source_checkpoint:
        from .websocket_archive_checkpoint import audit_checkpoint
        checkpoint=audit_checkpoint(source,max_unverified_segments=0)
        if not checkpoint.get("within_unverified_suffix_limit"):
            raise ValueError("Missing or stale fully verified source checkpoint")
        source_proof=None
    else:
        source_proof=verify_chain(source)
    paths,docs=_read_chain(source)
    if not dest.exists():
        dest.mkdir(mode=0o700,parents=False)
    lock=dest/".mirror.lock"
    with lock.open("a+b") as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        existing_paths,existing_docs=_read_chain(dest)
        if len(existing_paths)>len(paths):
            raise ValueError("Mirror has extra segments not in source chain")
        # Never trust existing mirror just because directory names match.
        # Compare *both* manifest bytes and compressed gzip bytes.
        if existing_paths:
            verify_seals(dest)
        for index,(mirror_path,source_path) in enumerate(zip(existing_paths,paths)):
            if (_sha(mirror_path/MANIFEST)!=_sha(source_path/MANIFEST)
                or _sha(mirror_path/PAYLOAD)!=_sha(source_path/PAYLOAD)):
                raise ValueError("Mirror prefix diverges from source evidence")
        count=0
        durable=True
        for src in paths[len(existing_paths):len(existing_paths)+max_segments]:
            staged=Path(tempfile.mkdtemp(prefix=".copy-",dir=dest))
            try:
                for name in (MANIFEST,PAYLOAD):
                    _copied_exact(src/name,staged/name)
                durable=_safe_fsync_dir(staged) and durable
                target=dest/src.name
                if target.exists():
                    raise FileExistsError("Mirror segment already exists")
                os.rename(staged,target)
                durable=_safe_fsync_dir(dest) and durable
                count+=1
            finally:
                if staged.exists():
                    shutil.rmtree(staged)
        # Actually reconstruct independent SQLite from *destination* gzip.
        # Full raw JSON, FK and temporal link verification, not file-level
        # hashes only. No writes to either originating live database.
        mirror_proof=verify_chain(dest)
        if len(_read_chain(dest)[0])==len(paths):
            if source_proof is not None:
                if any(mirror_proof[k]!=source_proof[k] for k in (
                    "segments","sessions","frames","books","trades",
                    "prior_book_trade_links",
                )):
                    raise ValueError("Complete mirrored restore differs from original")
            elif (mirror_proof["sessions"]!=sum(x["sessions"] for x in docs)
                  or mirror_proof["frames"]!=sum(x["frames"] for x in docs)):
                raise ValueError("Checkpoint source metadata differs from mirror recovery")
        sealed=verify_seals(dest)
        return {
            "status":("SEPARATE_MOUNT_MIRROR_RESTORED_AND_VERIFIED"
                      if require_other_mount else
                      "SIMULATED_MIRROR_RESTORED_NOT_INDEPENDENT"),
            "source":str(source),"mirror":str(dest),
            "source_segments":len(paths),
            "mirrored_segments":sealed["segments"],
            "new_segments_copied":count,
            "missing_source_segments_to_copy":len(paths)-sealed["segments"],
            "mirrored_compressed_bytes":sealed["compressed_bytes"],
            "mirror_recovery":mirror_proof,
            "mount_device_is_distinct":source.stat().st_dev!=dest.stat().st_dev,
            "same_machine_or_storage_hardware_independence_unverified":True,
            "offsite_disaster_recovery_proven":False,
            "directory_fsync_supported":durable,
            "original_archives_unchanged":True,
            "live_database_unchanged":True,
            "source_full_restore_skipped_using_checkpoint":use_verified_source_checkpoint,
            "destination_full_restore_performed":True,
            "automatic_sync_enabled":False,
            "real_orders_enabled":False,
        }


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="Manual public WebSocket archive volume mirror and full restore")
    parser.add_argument("--source",default="/home/hong/apps/FreeQuantLab/state/microstructure/incremental")
    parser.add_argument("--dest",default="/mnt/d/FreeQuantLab-Backups/websocket-incremental")
    parser.add_argument("--max-segments",type=int,default=MAX_COPY_SEGMENTS)
    parser.add_argument("--use-verified-source-checkpoint",action="store_true",
                        help="Opt-in: skip DUPLICATE source restore ONLY with fresh full checkpoint; destination always fully restored")
    parser.add_argument("--audit-seals",action="store_true")
    parser.add_argument("--verify-mirror",action="store_true")
    args=parser.parse_args(argv)
    if args.audit_seals:
        result=verify_seals(args.dest)
    elif args.verify_mirror:
        result=verify_chain(args.dest)
    else:
        destination=Path(args.dest)
        if not destination.parent.is_dir():
            raise FileNotFoundError("Mirror parent must be explicitly provisioned")
        result=mirror(source_root=args.source,dest_root=args.dest,
                      max_segments=args.max_segments,
                      use_verified_source_checkpoint=args.use_verified_source_checkpoint)
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

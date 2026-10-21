"""Append-only recovery checkpoints for PUBLIC WebSocket evidence chains.

CREATE: independently reconstitute whole SQLite and verify every raw frame
before sealing a checkpoint witness. FAST AUDIT: only manifest chain and gzip
SHA256 + prefix identity, NO SQLite restoration. Never turn a historical
checkpoint into a claim that today's new segments were fully recovered.

A checkpoint is a local *unsigned* witness. An adversary with write access to
the whole archive and checkpoints could forge both. Remote/offsite anchor is
NOT established. Never modifies WS producer or deletes evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from .websocket_archive import _fsync_directory,_sha
from .websocket_archive_seals import verify_seals
from .websocket_incremental_archive import _read_chain,verify_chain,MANIFEST

VERSION=1
MAX_CHECKPOINTS=50_000
CHECKPOINTS_FOLDER="checkpoints"


def _stable_json(value)->bytes:
    return (json.dumps(value,sort_keys=True,separators=(",",":"),
                       ensure_ascii=False)+"\n").encode()


def _prefix_fingerprint(paths:list[Path],count:int)->str:
    if type(count) is not int or count<1 or count>len(paths):
        raise ValueError("Invalid checkpoint prefix length")
    digest=sha256()
    for p in paths[:count]:
        digest.update((p.name+"\n").encode())
        digest.update(bytes.fromhex(_sha(p/MANIFEST)))
    return digest.hexdigest()


def _checkpoints_dir(root:Path)->Path:
    folder=root/CHECKPOINTS_FOLDER
    if folder.is_symlink():
        raise ValueError("Unsafe checkpoint directory symlink")
    return folder


def _existing(root:Path):
    folder=_checkpoints_dir(root)
    if not folder.exists():
        return []
    entries=sorted(folder.iterdir())
    if len(entries)>MAX_CHECKPOINTS:
        raise ValueError("Too many checkpoints")
    if any(not f.is_file() or f.is_symlink() or
           not f.name.startswith("verified-through-") or
           not f.name.endswith(".json") for f in entries):
        raise ValueError("Unexpected or unsafe checkpoint file")
    return entries


def _load(path:Path)->dict:
    data=json.loads(path.read_bytes())
    if (data.get("schema_version")!=VERSION
        or data.get("evidence")!="COMPLETE_SQLITE_RECONSTRUCTION_AT_CHECKPOINT_CREATION"
        or data.get("real_orders_enabled") is not False
        or data.get("independent_offsite_anchor") is not False
        or data.get("raw_frame_hashes_verified_at_creation") is not True):
        raise ValueError("Invalid checkpoint witness provenance")
    prefix=data.get("segment_count")
    if type(prefix) is not int or prefix<=0 or prefix>1_000_000:
        raise ValueError("Invalid checkpoint prefix count")
    for name in ("prefix_manifest_fingerprint_sha256","last_manifest_sha256"):
        h=data.get(name)
        if not isinstance(h,str) or len(h)!=64 or any(c not in "0123456789abcdef" for c in h):
            raise ValueError("Invalid checkpoint digest")
    if path.name!=f"verified-through-{prefix:06d}.json":
        raise ValueError("Checkpoint ordinal mismatch")
    return data


def create_checkpoint(root:str|Path)->dict:
    archive=Path(root).resolve()
    paths,docs=_read_chain(archive)
    if not paths:
        raise ValueError("No source archive segments")
    # ALWAYS full recovery, hashes and causal links before writing the proof.
    proof=verify_chain(archive)
    seals=verify_seals(archive)
    current_paths,current_docs=_read_chain(archive)
    if (len(current_paths)!=len(paths) or
        seals["archived_frames"]!=proof["frames"] or
        seals["archived_sessions"]!=proof["sessions"] or
        _sha(current_paths[-1]/MANIFEST)!=_sha(paths[-1]/MANIFEST)):
        raise ValueError("Archive changed during checkpoint creation; retry")
    checkpoint_dir=_checkpoints_dir(archive)
    checkpoint_dir.mkdir(mode=0o700,exist_ok=True)
    _existing(archive)
    final=checkpoint_dir/f"verified-through-{len(paths):06d}.json"
    if final.exists():
        old=_load(final)
        if old["prefix_manifest_fingerprint_sha256"]!=_prefix_fingerprint(paths,len(paths)):
            raise ValueError("Checkpoint prefix diverges; refusing replacement")
        return {
            "status":"EXISTING_FULL_CHECKPOINT_PRESERVED",
            "checkpoint":str(final),"checkpoint_sha256":_sha(final),
            "segments":len(paths),"whole_chain_recovered":True,
            "source_unchanged":True,
        }
    witness={
        "schema_version":VERSION,
        "evidence":"COMPLETE_SQLITE_RECONSTRUCTION_AT_CHECKPOINT_CREATION",
        "created_utc":datetime.now(timezone.utc).isoformat(),
        "source_db":docs[0]["source_db"],
        "segment_count":len(paths),
        "last_session_key":docs[-1]["last_session_key"],
        "last_manifest_sha256":_sha(paths[-1]/MANIFEST),
        "prefix_manifest_fingerprint_sha256":_prefix_fingerprint(paths,len(paths)),
        "compressed_bytes_through_checkpoint":seals["compressed_bytes"],
        "frames_through_checkpoint":proof["frames"],
        "sessions_through_checkpoint":proof["sessions"],
        "prior_book_trade_links_through_checkpoint":proof["prior_book_trade_links"],
        "raw_frame_hashes_verified_at_creation":True,
        "independent_offsite_anchor":False,
        "unsigned_same_machine_checkpoint":True,
        "real_orders_enabled":False,
        "live_source_not_modified":True,
    }
    with tempfile.NamedTemporaryFile(
        mode="wb",prefix=".checkpoint-stage-",dir=checkpoint_dir,
        delete=False
    ) as f:
        tmp=Path(f.name)
        os.chmod(tmp,0o600)
        try:
            f.write(_stable_json(witness))
            f.flush();os.fsync(f.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    try:
        if final.exists():
            raise FileExistsError("Checkpoint file cannot be overwritten")
        # Hardlink publication fails atomically if another author published
        # the same witness first; rename() might silently replace it.
        os.link(tmp,final,follow_symlinks=False)
        tmp.unlink()
        _fsync_directory(checkpoint_dir)
    finally:
        tmp.unlink(missing_ok=True)
    return {
        "status":"FULL_RECOVERY_CHECKPOINT_CREATED",
        "checkpoint":str(final),"checkpoint_sha256":_sha(final),
        "segments":len(paths),"sessions":proof["sessions"],
        "frames":proof["frames"],
        "full_recovery_performed":True,
        "source_unchanged":True,
        "offsite_independence_unverified":True,
    }


def audit_checkpoint(root:str|Path,*,max_unverified_segments:int|None=None)->dict:
    archive=Path(root).resolve()
    if max_unverified_segments is not None and (
        type(max_unverified_segments) is not int or
        max_unverified_segments<0 or max_unverified_segments>100_000
    ):
        raise ValueError("Invalid permitted unverified suffix size")
    paths,docs=_read_chain(archive)
    # Does NOT reconstruct any frames or SQLite; sealed gzip+manifest only.
    seals=verify_seals(archive)
    checkpoints=_existing(archive)
    if not checkpoints:
        return {
            "status":"NO_FULL_RECOVERY_CHECKPOINT",
            "segments":len(paths),"unverified_new_segments":len(paths),
            "gzip_and_manifest_SHA256":"PASS",
            "full_recovery_performed_in_this_audit":False,
            "checkpoint_creation_had_full_recovery":False,
            "checkpoint_is_signed_or_offsite":False,
            "within_unverified_suffix_limit":False,
        }
    witnesses=[(_load(f),f) for f in checkpoints]
    # One pass through immutable manifest identifiers. Even if thousands
    # of historical witnesses exist, this remains O(segments + checkpoints)
    # rather than re-hashing all earlier manifests for each witness.
    requested={w["segment_count"] for w,_ in witnesses}
    prefix_digests={}
    digest=sha256()
    for n,p in enumerate(paths,1):
        digest.update((p.name+"\n").encode())
        digest.update(bytes.fromhex(_sha(p/MANIFEST)))
        if n in requested:
            prefix_digests[n]=digest.hexdigest()
    for witness,f in witnesses:
        n=witness["segment_count"]
        if n>len(paths):
            raise ValueError("Checkpoint witnesses missing segments")
        if witness["last_manifest_sha256"]!=_sha(paths[n-1]/MANIFEST):
            raise ValueError("Archived segment diverges from recovery checkpoint")
        if witness["prefix_manifest_fingerprint_sha256"]!=prefix_digests[n]:
            raise ValueError("Recovery checkpoint prefix fingerprint mismatch")
        if (witness["source_db"]!=docs[0]["source_db"] or
            witness["last_session_key"]!=docs[n-1]["last_session_key"]):
            raise ValueError("Recovered checkpoint session/source identity mismatch")
        if witness["sessions_through_checkpoint"]!=docs[n-1]["source_session_count_through_last"]:
            raise ValueError("Checkpoint cumulative sessions mismatch")
    latest,checkpoint_file=max(witnesses,key=lambda x:x[0]["segment_count"])
    new=len(paths)-latest["segment_count"]
    within=(max_unverified_segments is not None and
            new<=max_unverified_segments)
    return {
        "status":("LATEST_PREFIX_SEALED_NO_NEW_SEGMENTS"
                  if new==0 else "NEW_SEGMENTS_NEED_FULL_RECOVERY_CHECKPOINT"),
        "checkpoint":str(checkpoint_file),
        "checkpoint_sha256":_sha(checkpoint_file),
        "checkpoint_created_utc":latest["created_utc"],
        "fully_recovered_at_checkpoint_segments":latest["segment_count"],
        "currently_sealed_segments":len(paths),
        "unverified_new_segments":new,
        "fully_recovered_at_checkpoint_frames":latest["frames_through_checkpoint"],
        "current_frames_count_by_manifest":seals["archived_frames"],
        "gzip_and_manifest_SHA256":"PASS",
        "full_recovery_performed_in_this_audit":False,
        "checkpoint_creation_had_full_recovery":True,
        "within_unverified_suffix_limit":within,
        "checkpoint_is_signed_or_offsite":False,
        "real_orders_enabled":False,
    }


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="Fast seal audit and witnessed FULL recovery checkpoint")
    parser.add_argument("--root",default="/home/hong/apps/FreeQuantLab/state/microstructure/incremental")
    parser.add_argument("--create",action="store_true")
    parser.add_argument("--max-unverified",type=int,default=None)
    args=parser.parse_args(argv)
    if args.create and args.max_unverified is not None:
        raise ValueError("max-unverified is for audit mode only")
    result=create_checkpoint(args.root) if args.create else audit_checkpoint(
        args.root,max_unverified_segments=args.max_unverified)
    print(json.dumps(result,ensure_ascii=False))
    if args.max_unverified is not None and not result["within_unverified_suffix_limit"]:
        return 2
    return 0


if __name__=="__main__":
    raise SystemExit(main())

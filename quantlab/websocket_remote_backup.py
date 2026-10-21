"""Portable append-only WS archive transfer, with independent recovery on receiver.

A transfer bundle contains ONLY new immutable incremental segment folders plus
base-prefix and compressed SHA checks. Source proof is fully restored before
publication; receiver verifies its own prefix, never overwrites an existing
segment and ALWAYS reconstructs complete SQLite after receiving.

This module is transport-neutral. Hand bundles to a separately authorized
SSH/SFTP/NAS transport; using it on the same WSL host is only a *simulation*
of a second machine's acceptance, NOT offsite backup evidence.
No live collector/DB, real orders, cleanup or scheduler.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from .websocket_archive import _sha,_fsync_directory
from .websocket_archive_seals import verify_seals
from .websocket_incremental_archive import (
    _read_chain, _read_manifest, verify_chain, MANIFEST, PAYLOAD,
)

BUNDLE_FILE="transfer.json"
MAX_SEGMENTS=12
BUNDLE_KIND="PUBLIC_WS_REMOTE_TRANSFER_V1"
SEGMENT_PATTERN=re.compile(r"^seg-[0-9]{6}$")


def _manifest_digest(path:Path)->str:
    return _sha(path/MANIFEST)


def _copy_file_exact(src:Path,dest:Path)->None:
    if src.is_symlink() or not src.is_file():
        raise ValueError("Unsafe evidence file")
    if dest.exists() or dest.is_symlink():
        raise FileExistsError("Existing evidence cannot be overwritten")
    with src.open("rb") as reader,dest.open("xb") as writer:
        os.chmod(dest,0o600)
        shutil.copyfileobj(reader,writer,length=1024*1024)
        writer.flush(); os.fsync(writer.fileno())
    if src.stat().st_size!=dest.stat().st_size or _sha(src)!=_sha(dest):
        raise ValueError("Transfer copy SHA differs")


def _bundle_doc(path:Path)->dict:
    file=path/BUNDLE_FILE
    if file.is_symlink() or not file.is_file():
        raise ValueError("Missing or unsafe transfer bundle manifest")
    if file.stat().st_size>65536:
        raise ValueError("Oversized transfer manifest")
    doc=json.loads(file.read_bytes())
    if (doc.get("kind")!=BUNDLE_KIND or doc.get("version")!=1
        or doc.get("private_account_data_included") is not False
        or doc.get("real_orders_enabled") is not False
        or doc.get("source_deleted") is not False):
        raise ValueError("Unsafe transfer bundle provenance")
    items=doc.get("segments")
    base=doc.get("base_segment_count")
    if type(base) is not int or base<0 or type(items) is not list or not 1<=len(items)<=MAX_SEGMENTS:
        raise ValueError("Invalid bounded transfer range")
    if (base==0 and doc.get("base_manifest_sha256") is not None or
        base>0 and not _digest_format(doc.get("base_manifest_sha256"))):
        raise ValueError("Invalid transfer base head")
    if not isinstance(doc.get("source_db"),str) or not doc["source_db"].startswith("/"):
        raise ValueError("Missing provenance")
    for n,item in enumerate(items,base+1):
        if (not isinstance(item,dict) or item.get("name")!=f"seg-{n:06d}"
            or not _digest_format(item.get("manifest_sha256"))
            or not _digest_format(item.get("gzip_sha256"))
            or type(item.get("gzip_bytes")) is not int or item["gzip_bytes"]<=0):
            raise ValueError("Invalid consecutive transfer segment")
    return doc


def _digest_format(s)->bool:
    return isinstance(s,str) and bool(re.fullmatch("[0-9a-f]{64}",s))


def _snapshot_bundle(source:Path,out:Path,*,base_segment_count:int,
                     max_segments:int)->dict:
    if type(base_segment_count) is not int or base_segment_count<0:
        raise ValueError("Invalid target prefix count")
    if type(max_segments) is not int or not 1<=max_segments<=MAX_SEGMENTS:
        raise ValueError("Invalid transfer batch size")
    # This is a FULL source reconstruction before authorization to transfer.
    proof=verify_chain(source)
    paths,docs=_read_chain(source)
    if base_segment_count>len(paths):
        raise ValueError("Target ahead of source")
    selected=paths[base_segment_count:base_segment_count+max_segments]
    if not selected:
        return {"status":"NO_NEW_VERIFIED_SEGMENTS_TO_EXPORT",
                "existing_segments":len(paths),
                "source_full_restored":True}
    if out.exists() or out.is_symlink():
        raise FileExistsError("Transfer outbox must be a new directory")
    if not out.parent.is_dir():
        raise FileNotFoundError("Transfer parent must exist")
    stage=Path(tempfile.mkdtemp(prefix=".transfer-staging-",dir=out.parent))
    try:
        staged_items=[]
        for path in selected:
            target=stage/path.name
            target.mkdir(mode=0o700)
            for file in (MANIFEST,PAYLOAD):
                _copy_file_exact(path/file,target/file)
            manifest=_read_manifest(target)
            staged_items.append({
                "name":path.name,
                "manifest_sha256":_sha(target/MANIFEST),
                "gzip_sha256":_sha(target/PAYLOAD),
                "gzip_bytes":manifest["gzip_bytes"],
            })
        doc={
            "version":1,"kind":BUNDLE_KIND,
            "created_utc":datetime.now(timezone.utc).isoformat(),
            "source_db":docs[0]["source_db"],
            "base_segment_count":base_segment_count,
            "base_manifest_sha256":(_manifest_digest(paths[base_segment_count-1])
                                    if base_segment_count else None),
            "segments":staged_items,
            "full_source_sqlite_restored_at_export":True,
            "full_source_frames_at_export":proof["frames"],
            "remote_transport_validated":False,
            "private_account_data_included":False,
            "real_orders_enabled":False,
            "source_deleted":False,
        }
        with (stage/BUNDLE_FILE).open("xb") as f:
            os.chmod(stage/BUNDLE_FILE,0o600)
            f.write((json.dumps(doc,sort_keys=True,separators=(",",":"))+"\n").encode())
            f.flush();os.fsync(f.fileno())
        # Protect against a mutable source changing while the bundle was read.
        later=_read_chain(source)[0]
        if len(later)<len(paths) or any(
            _manifest_digest(a)!=_manifest_digest(b)
            for a,b in zip(paths,later)
        ):
            raise ValueError("Source chain changed during export")
        _fsync_directory(stage)
        os.rename(stage,out)
        _fsync_directory(out.parent)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return {
        "status":"INDEPENDENT_TRANSPORT_BUNDLE_SEALED",
        "bundle":str(out),
        "source_segments":len(paths),"base_segment_count":base_segment_count,
        "exported_segments":len(selected),
        "source_full_restored_at_export":True,
        "transport_performed":False,
        "offsite_recovery_proven":False,
    }


def create_bundle(*,source_root:str|Path,outbox:str|Path,
                  base_segment_count:int,max_segments:int=MAX_SEGMENTS)->dict:
    source=Path(source_root).resolve()
    dest=Path(outbox).absolute()
    if not source.is_dir():
        raise FileNotFoundError("Source incremental archive missing")
    if dest==source or source in dest.parents or dest in source.parents:
        raise ValueError("Bundle must be outside source tree")
    return _snapshot_bundle(source,dest,base_segment_count=base_segment_count,
                            max_segments=max_segments)


def receiver_status(target_root:str|Path)->dict:
    target=Path(target_root).absolute()
    if target.is_symlink():
        raise ValueError("Unsafe receiver target")
    if not target.exists():
        return {
            "status":"EMPTY_RECEIVER_NOT_YET_PROTECTED","segment_count":0,
            "last_manifest_sha256":None,"full_restored_now":False,
        }
    if not target.is_dir():
        raise ValueError("Invalid receiver target")
    paths,docs=_read_chain(target)
    if paths:
        verify_seals(target)
    return {
        "status":"RECEIVER_PREFIX_SEALED_NOT_FULLY_RESTORED_NOW",
        "segment_count":len(paths),
        "last_manifest_sha256":_manifest_digest(paths[-1]) if paths else None,
        "full_restored_now":False,
        "offsite_hardware_proven":False,
    }


def _validate_transferred_bundle(path:Path,doc:dict)->list[Path]:
    expected={BUNDLE_FILE,*[x["name"] for x in doc["segments"]]}
    actual={entry.name for entry in path.iterdir()}
    if expected!=actual:
        raise ValueError("Extra, missing or unsafe transferred files")
    files=[]
    for item in doc["segments"]:
        seg=path/item["name"]
        if seg.is_symlink() or not seg.is_dir():
            raise ValueError("Unsafe transfer segment directory")
        if {p.name for p in seg.iterdir()}!={MANIFEST,PAYLOAD}:
            raise ValueError("Unexpected transferred segment files")
        src_manifest=seg/MANIFEST
        src_payload=seg/PAYLOAD
        if src_manifest.is_symlink() or src_payload.is_symlink():
            raise ValueError("Unsafe transferred symlink")
        manifest=_read_manifest(seg)
        if manifest["source_db"]!=doc["source_db"]:
            raise ValueError("Transferred source provenance mismatch")
        if (_sha(src_manifest)!=item["manifest_sha256"]
            or _sha(src_payload)!=item["gzip_sha256"]
            or src_payload.stat().st_size!=item["gzip_bytes"]
            or manifest["gzip_bytes"]!=item["gzip_bytes"]
            or manifest["payload_sha256"]!=item["gzip_sha256"]):
            raise ValueError("Transferred segment hash mismatch")
        files.append(seg)
    return files


def accept_bundle(*,bundle_dir:str|Path,target_root:str|Path)->dict:
    bundle=Path(bundle_dir).absolute()
    target=Path(target_root).absolute()
    if not bundle.is_dir() or bundle.is_symlink():
        raise ValueError("Unsafe or missing incoming bundle")
    if target==bundle or target in bundle.parents or bundle in target.parents:
        raise ValueError("Target and inbox must be isolated")
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError("Unsafe receiver target")
    if not target.parent.is_dir():
        raise FileNotFoundError("Receiver's parent must be pre-provisioned")
    doc=_bundle_doc(bundle)
    incoming=_validate_transferred_bundle(bundle,doc)
    target.mkdir(mode=0o700,exist_ok=True)
    with (target/".incoming.lock").open("a+b") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        old,old_docs=_read_chain(target)
        if old:
            verify_seals(target)
        base=doc["base_segment_count"]
        if (len(old)<base
            or (base and _manifest_digest(old[base-1])!=doc["base_manifest_sha256"])):
            raise ValueError("Receiver prefix does not match exported base")
        if old and old_docs[0]["source_db"]!=doc["source_db"]:
            raise ValueError("Receiver source provenance mismatch")
        added=[]
        try:
            for item,src in zip(doc["segments"],incoming):
                n=int(item["name"][-6:])
                if n<=len(old):
                    prior=old[n-1]
                    if (_sha(prior/MANIFEST)!=item["manifest_sha256"]
                        or _sha(prior/PAYLOAD)!=item["gzip_sha256"]):
                        raise ValueError("Divergent receiver segment; never overwrite")
                    continue
                if n!=len(old)+len(added)+1:
                    raise ValueError("Receiver segment sequence has a hole")
                stage=Path(tempfile.mkdtemp(prefix=".incoming-seg-",dir=target))
                try:
                    for f in (MANIFEST,PAYLOAD):
                        _copy_file_exact(src/f,stage/f)
                    _fsync_directory(stage)
                    published=target/item["name"]
                    if published.exists():
                        raise FileExistsError("Existing published receiver segment")
                    os.rename(stage,published)
                    _fsync_directory(target)
                    added.append(published)
                finally:
                    if stage.exists():shutil.rmtree(stage)
            # Receiver must be independently able to reconstruct the ENTIRE
            # SQLite and audit frame hashes, causality, unique IDs and FK.
            proof=verify_chain(target)
        except Exception:
            # Roll back ONLY segments this invocation created; existing
            # immutable mirror prefixes remain preserved even on corruption.
            for p in reversed(added):
                shutil.rmtree(p)
            _fsync_directory(target)
            raise
    return {
        "status":"RECEIVER_INDEPENDENT_SQLITE_RESTORED_AND_VERIFIED",
        "target":str(target),
        "newly_received_segments":len(added),
        "archived_segments":proof["segments"],
        "sessions":proof["sessions"],
        "frames":proof["frames"],
        "prior_book_trade_links":proof["prior_book_trade_links"],
        "raw_SHA256_all_frames_verified":True,
        "source_database_untouched":True,
        "offsite_independence_verified_by_this_module":False,
        "real_orders_enabled":False,
    }


def main(argv=None)->int:
    p=argparse.ArgumentParser(description="Manual, transport-neutral, verified public WS backup bundles")
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare",action="store_true")
    group.add_argument("--receive",action="store_true")
    group.add_argument("--status",action="store_true")
    p.add_argument("--source",default="/home/hong/apps/FreeQuantLab/state/microstructure/incremental")
    p.add_argument("--out",default=None)
    p.add_argument("--base-count",type=int,default=0)
    p.add_argument("--max-segments",type=int,default=MAX_SEGMENTS)
    p.add_argument("--incoming",default=None)
    p.add_argument("--target",default=None)
    args=p.parse_args(argv)
    if args.prepare:
        if not args.out:raise ValueError("--prepare requires --out")
        result=create_bundle(source_root=args.source,outbox=args.out,
                             base_segment_count=args.base_count,max_segments=args.max_segments)
    elif args.receive:
        if not args.incoming or not args.target:raise ValueError("--receive requires --incoming and --target")
        result=accept_bundle(bundle_dir=args.incoming,target_root=args.target)
    else:
        if not args.target:raise ValueError("--status requires --target")
        result=receiver_status(args.target)
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

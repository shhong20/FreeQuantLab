"""Fast, NON-RESTORING integrity check of archived PUBLIC WebSocket segments.

Validates manifest hash chain, payload file sizes and compressed SHA256.
It is NOT an SQLite restore or a proof of inner frame authenticity.
For that use verify_chain(...). Intended for frequent disk-budget audits.
"""
from __future__ import annotations

from pathlib import Path
from .websocket_archive import _sha
from .websocket_incremental_archive import _read_chain, PAYLOAD


def verify_seals(root:str|Path)->dict:
    paths,docs=_read_chain(Path(root).resolve())
    if not paths:
        raise ValueError("No incremental archive segments")
    compressed=0
    for folder,doc in zip(paths,docs):
        payload=folder/PAYLOAD
        if payload.is_symlink() or not payload.is_file():
            raise ValueError("Unsafe or missing compressed incremental evidence")
        if payload.stat().st_size!=doc["gzip_bytes"] or _sha(payload)!=doc["payload_sha256"]:
            raise ValueError("Incremental payload SHA256 mismatch")
        compressed+=payload.stat().st_size
    return {
        "status":"MANIFEST_AND_COMPRESSED_HASHES_VERIFIED_NOT_RESTORED",
        "segments":len(paths),"archived_sessions":sum(d["sessions"] for d in docs),
        "archived_frames":sum(d["frames"] for d in docs),
        "compressed_bytes":compressed,
        "last_session_key":docs[-1]["last_session_key"],
        "no_sqlite_restore_performed":True,
        "inner_frame_sha_and_causal_links_not_revalidated":True,
        "no_raw_payloads_or_orders_deleted":True,
        "real_orders_enabled":False,
    }

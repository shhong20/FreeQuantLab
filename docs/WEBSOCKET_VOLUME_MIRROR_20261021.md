# FreeQuantLab | PUBLIC WebSocket evidence — second-volume mirror (2026-10-21)

## Results

The live FreeQuantLab collector retains its original SQLite database and its local append-only incremental archive. This change supports a separate **manual** mirror of those archived data on the Windows D: volume, without deleting, moving or stopping the source. The source and destination filesystem devices differ. The backup is **not offsite** and is not proven to reside on physically independent hardware.

Source directory: /home/hong/apps/FreeQuantLab/state/microstructure/incremental

Backup directory: /mnt/d/FreeQuantLab-Backups/websocket-incremental
Windows folder: D:\FreeQuantLab-Backups\websocket-incremental

### Verified using real archived Upbit observations

| Check | Measured result |
|---|---:|
| Source and destination mount device IDs differ | PASS |
| Mirrored incremental segments | 3/3 |
| Copied compressed payload | 1,572,246 bytes |
| Full restore from destination compressed files | PASS |
| SQLite sessions restored | 9 |
| Public WebSocket frames restored | 5,809 |
| Orderbook frames and market trade prints | 5,206 / 603 |
| Prior-book causal references (not customer fills) | 267 |
| Segment manifest chain and compressed-file SHA-256 | PASS |
| Independently restored raw frame SHA and foreign-key relationships | PASS |
| Directory fsync on mounted destination | Supported during test |
| Original live DB modified or truncated | NO |
| Automatic mirror / deletion / retention | DISABLED |

The Windows drive was reported by WSL as mode 0777. This archive contains PUBLIC market observations, not API credentials or account secrets. Windows ACLs and disk encryption are not established by this test.

## Implemented tools

- quantlab/websocket_archive_mirror.py — validates the original chain via full SQLite reconstruction, refuses same-mount or nested destination, copies ONLY previously unmirrored segments to a staging folder, compares byte hashes, publishes using atomic rename, then reconstructs SQLite from the DESTINATION chain and validates all frame SHA-256 and causality. Existing mirrored segments are never overwritten. Reruns append zero new segments if fully synchronized.
- quantlab/websocket_archive_seals.py — faster metadata+compressed SHA-256 check that does **not** reconstruct SQLite and is explicitly labeled MANIFEST_AND_COMPRESSED_HASHES_VERIFIED_NOT_RESTORED. This does not replace the full source/restore audit.
- quantlab/websocket_incremental_archive.py — cumulative session count validation changed from repeated summation (quadratic in number of manifests) to a linear-time prefix accumulator, with the same safety checks.

Live source capture, trade-execution prevention, baseline/Exaone/Qlib paper tracking and both REST/WS timers are unchanged. No automatic mirroring or source cleanup is enabled.

## Commands from the pinned FreeQuantLab operational runtime

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python

# Mirror source's sealed incremental segments to a separate mounted D volume.
# Copies only NEW segments, then performs a FULL SQLite restore check.
$PY -m quantlab.websocket_archive_mirror

# Fast seal/bytes audit (NOT a recovered database or raw frame audit).
$PY -m quantlab.websocket_archive_mirror --audit-seals

# Full offline SQLite reconstruction from D volume source payloads.
$PY -m quantlab.websocket_archive_mirror --verify-mirror

# Restore to a new file not equal to the live database. Never overwrite.
mkdir -p /tmp/freequant-ws-restore
$PY -m quantlab.websocket_incremental_archive --verify \
    --root /mnt/d/FreeQuantLab-Backups/websocket-incremental \
    --restore-to /tmp/freequant-ws-restore/recovered.sqlite3
```

## Validation and limits

Tests cover nested/symlink destination protection, same-filesystem rejection, missing source/mirror segments, corrupted gzip, divergent manifest, rerun without duplicate writes, newly appended source segments, simulated loss of the source archive folder with mirrored-only restore, and proof that fast checks cannot be mistaken for full recovery.

This is a SAME-COMPUTER secondary-volume copy, not a cloud or remote-NAS disaster recovery guarantee. Identical machine/hardware failure, theft or Windows loss could destroy both original and mirror; protecting against that requires a genuinely independent system. Copying completed segments does NOT protect sessions that have not yet been archived from the live SQLite source. No automatic archiving, mirroring, deleting or trading has been enabled.

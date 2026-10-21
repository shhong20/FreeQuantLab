# FreeQuantLab — WSL-to-D Incremental WebSocket Backup Automation (2026-10-21)

## Scope
This adds an opt-in, bounded, append-only hourly **systemd --user** backup cycle of PUBLIC Upbit WebSocket quotes and market trade prints. It copies verified finalized sessions from the live capture SQLite to a WSL immutable incremental archive, then mirrors the new compressed segments to a **separate filesystem mounted at Windows D:**. It always reconstructs SQLite from the mirror and checks every raw frame SHA-256 and causal book reference. It never deletes source files, fills missing quotes, trades on an exchange, promotes a model, or automatically contacts a remote NAS.

The mirror is on the SAME PHYSICAL COMPUTER. SSH remote backup separately remains a manual and unconfigured capability, not proof of offsite disaster recovery.

## Real-data first smoke test
On 2026-10-21 around 21:29–21:30 KST, the new cycle manually ran on the live WSL data. 2 newly completed sessions containing 1,157 original frames became 1 fresh immutable increment. Source and D mirror each held 9 segments / 34 sessions. Full reconstruction of the D mirror verified **22,968 original public WebSocket frames**, frame SHA and book/trade relationships, PASS. Finished-session backup gap immediately after the cycle was 0 on both WSL and D. New sessions accumulate during later automatic captures, so this is only a time-specific check.

Append-only success receipt: state/microstructure/backup-receipts/20261021T123001Z-0aa8b64307.json.

## Safety and bounded work
- Source SQLite opened read-only. Same-day UTC is a documented *partial date*, not a fully sealed day; unfinished capture sessions are never fabricated.
- Each run processes at most 6 batches of at most 20 finalized sessions. A reboot may leave a backlog larger than one cycle; a nonzero report keeps it visible until later runs catch up.
- Destination MUST already be a different filesystem device. Both storage volumes must have at least 2GiB free. A source compressed archive chain larger than 256MiB fails closed without deleting any evidence. This cap means long-running deployment eventually requires a more scalable immutable incremental restore/checkpoint method.
- Runs are locked against overlap; full source and full destination SQLite reconstructions are mandatory before publishing a successful append-only JSON receipt. Failure is reported through nonzero exit status and systemd journal.
- Runs every hour at minute 45 KST, with 15-minute boot catch-up and Persistent=true, via unprivileged user systemd. Resource bounds: Nice=10, IOSchedulingClass=idle, TimeoutStartSec=300.
- The previous REST/WS capture, watchdog, Exaone, Qlib, paper trading and disabled clean cohort units are not changed.

## Commands — no sudo required

From the pinned runtime, run read-only preflight before enabling, then install only the new user timer:

    cd /home/hong/apps/FreeQuantLab-observer-runtime
    PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
    $PY -m quantlab.websocket_backup_cycle
    $PY -m quantlab.websocket_backup_cycle --run
    mkdir -p ~/.config/systemd/user
    cp deploy/systemd/freequant-websocket-backup.service ~/.config/systemd/user/
    cp deploy/systemd/freequant-websocket-backup.timer ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now freequant-websocket-backup.timer
    systemctl --user start freequant-websocket-backup.service
    systemctl --user list-timers --all 'freequant-websocket-backup*'
    journalctl --user -u freequant-websocket-backup.service -n 50 --no-pager

Independent full SQLite restore from the D copy and the read-only lag report:

    $PY -m quantlab.websocket_archive_mirror --verify-mirror
    $PY -m quantlab.websocket_backup_gap

Disable the NEW backup timer without deleting any of the stored evidence:

    systemctl --user disable --now freequant-websocket-backup.timer

No root privileges are needed for a normal systemd user install. If a specific WSL permission failure actually appears, stop there; do not weaken source filesystem permissions or run backup as root.

## Real 24h readiness
Existing user timers already audit raw frame evidence, and rolling WS/REST 24h coverage gates automatically. The first WS session was at 2026-10-21 16:09 KST, so 24h can be judged only after 2026-10-22 16:09 KST. No historical orderbook samples can be backfilled from a later market observation.

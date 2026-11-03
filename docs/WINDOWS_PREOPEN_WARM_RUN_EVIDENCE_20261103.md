# Windows preopen warmup execution receipts — read-only sidecar (2026-11-03)

## Why add this

Host **DESKTOP-S51Q473** already has Windows Task Scheduler's `FreeQuantLab-WSL-PreOpen-Warm` at **08:40 / 09:05 KST**, independent WSL readiness at **08:46 / 09:07 / 09:12**, clean paper decision at **09:24**, a cumulative clean-cohort watchdog at **10:50 / 12:50**, and a day-specific postopen audit at **10:55 / 12:55**. All real clean 90-day paper records remain under the frozen `d6b531e` operations tree.

Windows's `Microsoft-Windows-TaskScheduler/Operational` event log is **disabled** (verified 2026-11-03). Therefore, at 10:55 `Get-ScheduledTaskInfo` exposes only the most recent 09:05 task outcome, not independent evidence of the earlier 08:40 execution. A configured schedule or WSL-side readiness timestamp alone **does not prove** Windows actually ran the warmup.

## New sidecar work — no changes to the original warmup task

- `ops/windows/wsl_preopen_warm_capture.ps1` is a read-only Windows task state capture, locally installed at `%LOCALAPPDATA%\FreeQuantLab\ops\wsl_preopen_warm_capture.ps1` so its scheduled action does **not depend on an already-running WSL UNC path**.
- `ops/windows/wsl_preopen_receipt_task.ps1` installs/independently audits a **second user-scoped** Windows task named `FreeQuantLab-WSL-Warm-Receipt`, with two daily runs at **08:50 and 09:10 KST**. Default mode is **Audit**; only an explicit `-Mode Install -SourcePath ...` creates the new task.
- Task runs as the *same logged-in limited user* with `WakeToRun=false`. It never changes the original 08:40/09:05 warm action, user login task, resume task, power policy, Upbit source, research DB, Qlib/Exaone, old 90-day protocol or broker state.
- Each run queries **existing** `Get-ScheduledTask` / `Get-ScheduledTaskInfo` from within its own finite window:
  - 08:50–08:54 allows a recorded 08:40 warm task result at 08:39–08:50.
  - 09:10–09:14 allows a recorded 09:05 warm task result at 09:04–09:10.
  - A nonzero Windows exit result, invalid task configuration/Windows SID, nonmatching last-run time, late catch-up, or task absent is **not marked successful**.
- Writes an append-only timestamped JSON + SHA-256 alongside itself in `%LOCALAPPDATA%\FreeQuantLab\preopen_warm_audit\`. It does not expose passwords, tokens, market prices, or private account numbers. A Windows user who can edit these files can also rewrite the hash; **SHA detects ordinary corruption, not malicious user-level forgery or TPM-backed Windows attestation**.
- If Windows is powered off/Windows user not logged in/WSL won't boot, **do not recreate old warm task results**. TaskScheduler catch-up outside these windows cannot claim success or fabricate market observations.

## WSL integration

`quantlab.clean_windows_warm_chain` verifies both Windows task-state receipts **independently** against Windows timestamps/sha256/result-code and correlates:
- Windows 08:40/09:05 action last-run status,
- WSL 08:46/09:07/09:12 SHA-verified readiness snapshots,
- frozen six-account 09:24 paper decision/quote evidence,
- the separate 90-day cumulative missing audit.

The existing `freequant-clean-postopen.service` runs one additional **read-only second ExecStart**. No new WSL systemd timer is needed. The new Windows→WSL proof is saved separately as immutable `windowschain-*.json(.sha256)` under the same private `state/postopen_chain/YYYY-MM-DD/` folder. Previous V1 `postopen-*.json` reports are unchanged and still independently re-verifiable.

Three layers have intentionally distinct meanings:

| Source | What it confirms | What it cannot confirm |
|---|---|---|
| Windows warm receipt | Correct task definition and an observed last run exiting 0 at permitted time | WSL stayed running, complete OS uptime, PC wake-from-sleep |
| WSL preopen receipt | User timers/frozen accounts/public API were ready at the recorded checkpoint | Windows tasks actually started |
| 90-day shadow cohort | Six account/day paper events, valid UTC dates, pinned public quotes | Real broker fills, realized return, complete historical days |

All three valid gives `WINDOWS_WARM_AND_DAILY_OBSERVATION_EVIDENCE`; partial warm evidence can still coexist with a valid day's six paper decisions and is explicitly `DAILY_OBSERVATION_WITH_PARTIAL_BOOT_EVIDENCE`. The 42 earlier account/day missing events **remain missing**, not filled or declared profitable. No strategy PnL, Sharpe or real broker order is calculated.

## Applied-source specific paths

Windows user `HONG`:

```text
C:\Users\HONG\AppData\Local\FreeQuantLab\ops\wsl_preopen_warm_capture.ps1
C:\Users\HONG\AppData\Local\FreeQuantLab\preopen_warm_audit\windowswarm-*.json
C:\Users\HONG\AppData\Local\FreeQuantLab\preopen_warm_audit\windowswarm-*.json.sha256
```

WSL observes the Windows source at:

```text
/mnt/c/Users/HONG/AppData/Local/FreeQuantLab/preopen_warm_audit
```

Run installation once **after reviewed PR merge and CI**:

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
SRC="$(wslpath -w "$PWD/ops/windows/wsl_preopen_warm_capture.ps1")"
INSTALL="$(wslpath -w "$PWD/ops/windows/wsl_preopen_receipt_task.ps1")"

/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTALL" -Mode Install -SourcePath "$SRC"
/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTALL" -Mode Audit -SourcePath "$SRC"
```

Only install if no same-name task or if its complete verified definition matches; never overwrite mismatched tasks or scripts. The 08:50/09:10 *first real scheduled executions* are only verifiable on 2026-11-04 or later, not during 2026-11-03 development.

## Operational validation

```bash
export XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
systemctl --user status freequant-clean-postopen.service
systemctl --user list-timers --all freequant-clean-postopen.timer
```

The next-day 10:55 postopen report will show both Windows individual results (if present), WSL three checks (if present), and the actual six clean cohort records. It cannot start a powered-off Windows computer or save a paper decision after the valid 09:24 observation window has closed.

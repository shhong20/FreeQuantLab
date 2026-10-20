# WSL delayed timer, frozen authorization failure, and stale watchdog guard

Verified 2026-10-20 KST on DESKTOP-S51Q473. The original six-account forward protocol, Exaone two-account frozen factor and Qlib two-account frozen models are unchanged. **Only virtual observation** is permitted; there are no real orders or paper backfills.

## 1. WSL resumed too late: no stale virtual fills

The isolated `freequant-exaone.timer` and `freequant-qlib-lab.timer` use `Persistent=true`. This recovers the service activation after WSL was stopped, but should **not** authorize recording a hypothetical fill using a public ticker acquired hours after the registered opening time.

`isolated_scheduler.eligible_decision` now permits only **UTC 00:10–01:19**, or **09:10–10:19 KST**, on the next-day UTC candle. The configured scheduled attempts themselves remain at 09:18/09:28/09:38/10:05 (Exaone) and 09:21/09:31/09:41/10:08 (Qlib). At 10:20 KST or later, or before 09:10 KST, automated catch-up returns `OUTSIDE_AUTHORIZED_UTC_WINDOW`, **zero simulated virtual trades**, and no ticker request. This does not alter the original static strategy plan or its SHA. The original six-account protocol has its own unchanged scheduler.

If the machine was offline during the window, the missing observations stay explicitly missing. The independent watchdog reports `ISOLATED_DUE_DAY_MISSING` after 10:20 KST, never backfills.

Regression tests use injected delayed-timer timestamps: 09:09:59, 10:20, 12:45 and a following-day noon; each rejects both lanes even with a runner that would otherwise mutate the ledger. The exact allowed boundary 09:10 through 10:19 is tested.

## 2. Failed frozen authorization creates a durable critical alert

Previously a deleted or altered `ops/isolated_paper_scheduler.json` caused the independent watchdog to crash before writing to its alert database. Now a policy-read/parse/SHA verification failure returns `DEGRADED`, `ISOLATED_AUTOMATION_POLICY_INVALID`, critical severity, no falsely trusted strategy hash and no exposed financial results.

The read-only inspection does not open an untrusted policy-derived account path in this case. `check_isolated` saves the incident to **independent** `state/isolated-watchdog.db`, publishes `state/isolated-watchdog-history.json`, and produces a daily report. Repeated identical faults do not duplicate alerts. Restoring the original frozen plan resolves that alert, provided the account/ledger/model integrity checks pass. This feature does **not** rewrite or repair a tampered authorization.

Tests deliberately tamper the policy's broker permission, delete the file, check an alarm can be persisted, then restore the original file and verify a recovery transition. No real account updates.

## 3. A stale dashboard report is not a healthy current status

The independent watchdog normally runs 10:35, 12:35 and 18:35 KST. An evening-to-next-morning interval can be 16 hours, so we set a conservative **18-hour freshness limit** for the most recent independent report, plus rejection of timestamps over five minutes in the future.

Read-only endpoint `GET /api/isolated-status`:
- 200 only when report is structurally valid and recent.
- 503 with `STALE_ISOLATED_WATCHDOG` and the last-checked timestamp if stale or significantly in the future.
- 503 `unavailable` if required timestamp is missing, malformed or timezone-unaware.

The frontend displays **감시 실행 지연** rather than implying the last archived `COLLECTING` result remains healthy. It never displays a stale alert as verified financial performance.

These endpoints do not wake Windows or guarantee that the desktop is continuously online. The Windows host must stay awake and WSL must remain running for the 09:00–10:35 KST operating period.

## Final read-only operator commands

```bash
cd /home/hong/apps/FreeQuantLab
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user list-timers --all 'freequant*' --no-pager
systemctl --user --failed --no-pager
.venv/bin/python -m quantlab.lane_comparison
.venv/bin/python -m quantlab.qlib_lab status \
  --plan protocols/2026-10-20-qlib-lab.json --db state/qlib_lab_shadow.db
curl -i http://127.0.0.1:8788/api/isolated-status
journalctl --user -u freequant-exaone.service -u freequant-qlib-lab.service \
  -u freequant-isolated-watchdog.service -n 60 --no-pager
```

No real market fill, live brokerage execution, future return or unattended machine power-management guarantee is claimed.

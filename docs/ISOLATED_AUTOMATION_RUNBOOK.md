# Automatic isolated Exaone/Qlib forward-paper observations · WSL runbook

**Registered:** 2026-10-20 UTC. **Research only.** No live exchange orders, portfolio promotion, training or retrospective virtual fills.

## Why an additional frozen operational authorization exists

The original frozen research plans explicitly said `manual_only=true` / `automatic_runner_enabled=false`, meaning no automatic job was configured at registration. The user subsequently authorized **automated *virtual-only* forward observations** for those exact existing research accounts. We DO NOT modify/re-sign the original Exaone or Qlib model/strategy manifests or the six-account preregistered 90-day protocol.

Instead, `ops/isolated_paper_scheduler.json` is a separately SHA-256-sealed, Git-versioned authorization referencing the **exact unchanged original manifest SHA-256 hashes**, database names and fixed observability window. It authorizes only the operation of the two frozen virtual accounts per lane. No re-proposal, Qlib retraining, model swap, automatic strategy promotion, historic virtual order backfill or real broker execution.

This operational change **does not retroactively blind** the 90-day study or turn separately registered model candidates into a pre-specified confirmatory alpha test.

## Timers (Asia/Seoul; daily)

| Systemd unit | First attempt | Additional checks | Scope |
| --- | --- | --- | --- |
| `freequant-shadow.timer` **existing** | 09:15 | 09:25, 09:35, 10:00 | Original preregistered 6 accounts; never touched |
| `freequant-exaone.timer` **new** | 09:18 | 09:28, 09:38, 10:05 | Only 2 frozen Exaone rule-factor virtual accounts |
| `freequant-qlib-lab.timer` **new** | 09:21 | 09:31, 09:41, 10:08 | Only 2 frozen Qlib LightGBM checkpoint virtual accounts |
| `freequant-watchdog.timer` **existing** | 10:30 | 12:30, 18:30 | Original 90-day study alerts and reports |
| `freequant-isolated-watchdog.timer` **new** | 10:35 | 12:35, 18:35 | Independent Exaone/Qlib due-date and ledger/model audits |

A UTC day closes at **09:00 KST the following Korean day**. `OnCalendar` times were checked via `systemd-analyze calendar` on the actual WSL timezone. A daily decision is due for isolated lanes only **after 10:20 KST on the next day** (last retries until 10:08).

The new operational authorization covers UTC decision dates **2026-10-20 through 2027-01-18**, inclusive. Thus the first *possible* simulated observation is for completed **UTC 2026-10-20**, checked from **2026-10-21 09:18 KST** onward. The original six-account preregistered scoring period remains **UTC 2026-10-21..2027-01-18**. Its results are still locked until 2027-01-19 00:00 UTC.

Systemd `Persistent=true` may trigger a timer after WSL resumes, but it cannot backfill previous decision days: the underlying asset data and ledger only accept **yesterday's fully completed UTC day** (if eligible), one event per frozen account/day. Past missed days stay marked missing in the independent watchdog.

## Guard chain before each virtual tick

1. Verify operational authorization SHA-256, exact UTC window, separate DB locations and **real_orders_enabled=false**.
2. Verify old frozen Exaone/Qlib plan SHA-256, stable account IDs and risk settings and original static model source/LightGBM hashes. Runtime Exaone inference uses **only the already generated factor**, and never calls Ollama again.
3. Read-only forensic audit of paper cash/units/fees and public Upbit quote evidence. Any missing quote or inconsistent price blocks the run; for Qlib also independently recompute saved model decisions from exact frozen nine-feature inputs.
4. Execute the existing forward-only observed-candle virtual runner on its **own** SQLite DB. The Qlib model, threshold .55, original BTC/ETH strategy configs and account identities never change. No real broker endpoint, credentials or auto-promotion logic is used.
5. On retries, existing observed virtual events remain idempotent, and no earlier missing dates are synthesized.

If any guard or engine call fails, the one-shot service exits nonzero. It does not retry indefinitely outside the scheduled windows.

## Independent operational monitoring

`quantlab.isolated_watchdog` reads Exaone and Qlib data **read-only**. It validates both frozen plans and model/quote evidence, the most recent run status, and counts due/recorded/missing UTC days by asset and lane.

- Due date not recorded: `ISOLATED_DUE_DAY_MISSING` warning after 10:20 KST, **no false alarms before**.
- Last virtual cycle ERROR: `ISOLATED_LAST_CYCLE_ERROR` warning.
- Frozen plan/ledger/quote/model replay unavailable or contradictory: `ISOLATED_LANE_INTEGRITY_FAILED` critical.
- Session or UTC decision timestamp anomaly: `ISOLATED_EVENT_SESSION_INVALID` critical.

The new alert persistence is **fully separate** from `state/watchdog.db`: `state/isolated-watchdog.db`, `state/isolated-watchdog-history.json`, and `reports/isolated/YYYY-MM-DD.json`. Repeat notifications are deduplicated; resolved issues are logged. Windows Toast notification submission is best effort and may fail even when local alert persistence succeeds. Missing virtual decisions remain missing.

Loopback-only read-only dashboard:
`GET http://127.0.0.1:8788/api/isolated-status` (latest **timestamped** snapshot)
and `GET http://127.0.0.1:8788/api/isolated-alerts`.
These never show held-out 90-day return scores or enable writes.

## Operator commands

```bash
cd /home/hong/apps/FreeQuantLab
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus

# Three new timers; do NOT overwrite the original two timers:
systemctl --user status freequant-exaone.timer freequant-qlib-lab.timer \
  freequant-isolated-watchdog.timer --no-pager
systemctl --user list-timers --all 'freequant*' --no-pager
journalctl --user -u freequant-exaone.service -u freequant-qlib-lab.service \
  -u freequant-isolated-watchdog.service -n 60 --no-pager

# Safe current-day smoke tests (no fill when no new completed UTC candle).
.venv/bin/python -m quantlab.isolated_scheduler exaone
.venv/bin/python -m quantlab.isolated_scheduler qlib
.venv/bin/python -m quantlab.isolated_watchdog --state-db \
  state/isolated-watchdog-smoke.db --report-dir reports/isolated-smoke

# Disable NEW isolated experimental schedules without altering original timers:
systemctl --user disable --now freequant-exaone.timer \
  freequant-qlib-lab.timer freequant-isolated-watchdog.timer
```

A disabled WSL or offline desktop prevents time-based execution; persistent timers do not guarantee delivery while Windows/WSL is unavailable. Check the latest watchdog report timestamp rather than assuming continuous online monitoring.

## Integrity limitations

The quote is public Upbit **last-traded ticker**, not executable ask/bid. Virtual trades and assumed fees/slippage are not real fill quality or investable alpha. Due-day completeness is only one prerequisite for descriptive future comparisons. We deliberately do not auto-promote the Qlib or Exaone models, revise the original blinded-claim flags, aggregate fictitious capital across independent accounts, or place real orders.

**Validation:** systemd unit syntax & Seoul calendar checks, pure Python policy/guard tests, missing-day/dedup/recovery alarm tests, read-only dashboard tests, fresh-model Qlib replay integration CI, and WSL read-only audits of all three independent ledgers.

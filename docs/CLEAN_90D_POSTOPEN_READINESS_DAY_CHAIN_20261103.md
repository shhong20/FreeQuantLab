# Clean 90-day: independent preopen → paper decision → postopen evidence chain

2026-11-03, WSL DESKTOP-S51Q473, read-only study. No live brokerage actions.

## Confirmed operating issue and separation of two statuses

On 2026-11-03 KST the original frozen BTC/ETH × 3-strategy clean paper cohort completed **all six due account-day decisions** for the 2026-11-02 UTC decision candle. The original source DB has a frozen valid event for each account, 2 independently pinned public quote rows, and four in-window run receipts.

However, the existing `freequant-clean-watchdog.service` returned exit status 2 at 10:50 and 12:50 KST, because it **correctly** requires the entire prospective cohort from 2026-10-22 to have all due decisions. Earlier machine downtime/missed windows created **42 cumulative account-day gaps**: `30 valid / 72 due`, so its cumulative result is legitimately `INCOMPLETE`.

Do **NOT** silence the cumulative alert, change its exit status or invent old fills. Instead add a **separate original-source day-level result** while keeping historical compliance visible.

## Implementation

Module: `quantlab.clean_postopen_chain`
- Read-only reconstruction via `quantlab.clean_90d_evidence_coverage.build`: frozen protocol identity, 6 accounts, original SQLite shadow event rows, 2 public quote records per complete UTC day, valid quote timestamps and executed within 00:10–01:20 UTC, real orders = 0.
- Today's KST report date corresponds to **previous UTC decision date** (2026-11-03 KST → 2026-11-02 decision). At/after **10:50 KST** a full day is only `SIX_OF_SIX_VALID_ORIGINAL_FORWARD_DECISIONS` with six independently verified account-day event records. A top-level `shadow_runs.status=OK` without 6 account events is **not** a success.
- Also checks SHA-256/permissions/identity for preopen readiness receipts (`state/preopen_readiness/readiness-*.json[.sha256]`), and explicitly bins into KST 08:40–08:59, 09:00–09:09, 09:10–09:15. All three are only declared successful when the frozen six accounts, five systemd timers and Upbit public KRW-BTC/KRW-ETH check reported ready. Records generated later are **late evidence**, not proof the 08:40 Windows task succeeded.
- Because the preopen timer was installed at ~12:01 KST on **2026-11-03**, all three scheduled preopen receipts can first exist on **2026-11-04**. Do not penalize **2026-11-03** for the absence of pre-installation receipts; the source report states `NOT_REQUIRED_BEFORE_PREOPEN_INSTALL_ROLLOUT`.
- The report states `win32_login_resume_tasks_independently_attested_from_this_report=false`: WSL readiness alone **cannot prove** a Windows scheduled task ran, PC was powered on throughout, or the host woke from sleep.
- Preserves two independent statuses:
  - Daily `TODAY_COMPLETE_* / TODAY_DECISION_INCOMPLETE_NO_BACKFILL`
  - Cumulative `INCOMPLETE_COHORT_COVERAGE_NO_STRATEGY_PERFORMANCE` with original expected/valid/missing/invalid numbers.
- No market prices, order parameters, prospective fills, performance/Sharpe, credentials or brokerage endpoints appear in the report.
- Privately saves immutable `report-*.json + report-*.json.sha256` under `state/postopen_chain/YYYY-MM-DD/` with directory `0700` and report `0600`. Independent reread verifies original shadow DB as-of the capture time and the original preopen receipts. If a report is tampered/rehashed to claim strategy approval, the independent reconstruction rejects it.

## Real 2026-11-03 audit

```text
KST report date:              2026-11-03
UTC decision date:            2026-11-02
Today account-day decisions:  6/6 valid
Today in-window run receipts: 4
Total due account-days:       72
Total valid:                  30
Total missing:                42
Total invalid:                0
Cumulative clean status:      INCOMPLETE (unchanged)
Windows morning readiness:    not required until 2026-11-04
New or backdated paper fills: 0
Broker orders:                0
Strategy returns/Sharpe:      null
```

One verified snapshot:
```
/home/hong/apps/FreeQuantLab/state/postopen_chain/2026-11-03/
    postopen-20261103T040456638811Z.json
    postopen-20261103T040456638811Z.json.sha256
```

## New user-systemd timer (research-only)

`deploy/systemd/freequant-clean-postopen.service`, `deploy/systemd/freequant-clean-postopen.timer`:
- 10:55 KST, after the existing 10:50 independent cumulative watchdog.
- 12:55 KST, after the existing 12:50 repeated watchdog.
- `OnStartupSec=13min` and `Persistent=true`: WSL restarting late produces a **late diagnostic**, not retroactive timed quotes/decisions. Existing accounts, stock history, old clean 90d branch and all existing user timers stay unchanged.
- Only writable filesystem path in the sandbox: `/home/hong/apps/FreeQuantLab/state/postopen_chain`.
- Systemd day-specific service status can succeed for today's complete observations **even if the legacy cumulative watchdog correctly returns nonzero due old missing days**. This does not suppress or overwrite existing watchdog alerts.

## Repeat validation
```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
P=/home/hong/apps/FreeQuantLab/.venv/bin/python
$P -m quantlab.clean_postopen_chain \
 --db /home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db \
 --protocol /home/hong/apps/FreeQuantLab-clean-runtime/protocols/2026-10-21-btc-eth-clean-90d.json \
 --preopen-dir /home/hong/apps/FreeQuantLab/state/preopen_readiness \
 --report-dir /home/hong/apps/FreeQuantLab/state/postopen_chain

# Independent verification only; replace with a concrete saved report:
$P -m quantlab.clean_postopen_chain \
 --db /home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db \
 --protocol /home/hong/apps/FreeQuantLab-clean-runtime/protocols/2026-10-21-btc-eth-clean-90d.json \
 --preopen-dir /home/hong/apps/FreeQuantLab/state/preopen_readiness \
 --report-dir /home/hong/apps/FreeQuantLab/state/postopen_chain \
 --verify-receipt /home/hong/apps/FreeQuantLab/state/postopen_chain/2026-11-03/postopen-20261103T040456638811Z.json
```

**Next check**: 2026-11-04 KST, actual Windows/WSL preopen receipts 08:40–09:12, actual 09:24 clean run, independent 10:50 cumulative watchdog and 10:55 day-level report. If Windows is powered off, the scheduler cannot run; the system records missing evidence after startup but cannot recreate a historical clean execution.

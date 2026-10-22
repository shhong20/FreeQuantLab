# Independent clean 90-day baseline cohort (registered 2026-10-21 UTC)

This cohort is research-only and has **no real orders, broker access, or automatic promotion**. Its virtual accounts do not share cash, positions, database tables, or account IDs with the contaminated original 90-day experiment. The original database and its six pre-window events remain untouched. A new experimental cohort does NOT retroactively repair or validate the original one.

## Frozen evidence and start date

- Registered at UTC `2026-10-21T01:57:29.374520+00:00`.
- Distinct ledger: `state/clean_shadow_90d.db`, with 6 accounts (BTC/ETH × buy/hold, trend MA20/80, factor r5), each initially KRW 1,000,000 and no position.
- Immutable protocol: `protocols/2026-10-21-btc-eth-clean-90d.json`.
- Protocol SHA-256: `6f5e9cf6333d3dd5fe4339ef9d7ba401df36bc361915584bf45226f464cc5054`.
- First eligible UTC decision: **2026-10-22**; last eligible: **2027-01-19**; first evaluation instant: **2027-01-20 00:00 UTC** (**09:00 KST**).
- First legitimate simulated observation is **2026-10-23 09:24 KST**, after the UTC 2026-10-22 candle has closed. The existing baseline is still expected to collect its first in-window day on **2026-10-22 09:15 KST**.
- All six account IDs are different from the original cohort. We changed only the **research eligibility setting** `paper_min_days: 30 -> 90`, via `examples/clean_shadow_crypto.json`; exposure, risk, signals, assumed fees/slippage and initial virtual capital are unchanged. Settings are part of the immutable account ID. Never modify that config/identity mid-experiment.
- Before correcting the identity collision, an initial attempt with zero events was archived under `state/clean_shadow_90d.registration-attempt-1.*`. It is evidence, not an evaluable cohort.

## Operations

All four dedicated user-systemd units are separate from original, Exaone and Qlib:

- `freequant-clean-shadow.timer/service`: KST 09:24, 09:34, 09:44, 10:10, with original protocol locked and database isolated.
- `freequant-clean-watchdog.timer/service`: KST 10:50 and 12:50, plus after systemd startup. It audits **all due account-days**, not only today's last row, along with SQLite replay, frozen identities, and saved public ticker consistency. Reports: `reports/clean-daily/YYYY-MM-DD.json`; local alert outbox: `state/clean-watchdog.db`.
- A late `Persistent=true` replay cannot create a simulated fill outside 09:10-10:19 KST. It saves only a skipped run. Market OHLCV catch-up does not fabricate missed paper orders.
- `WAITING_FIRST_DUE` is normal before the first eligible UTC candle is due. After 10:20 KST on 2026-10-23, missing account/day events, price discrepancies, foreign track IDs, pre-window fills, or ledger issues yield `INCOMPLETE` and a durable local alert.
- Locked returns remain `null` in clean receipt JSON. This is unblinded research, not confirmatory statistically independent alpha proof. No real execution quality can be inferred from Upbit public last-trade ticks.

Check:

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m quantlab.clean_cohort
.venv/bin/python -m quantlab --db state/clean_shadow_90d.db protocol-inspect \
  --protocol protocols/2026-10-21-btc-eth-clean-90d.json
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user list-timers --all 'freequant-clean*'
journalctl --user -u freequant-clean-shadow.service -u freequant-clean-watchdog.service -n 80 --no-pager
```

Never reset, delete, backfill, or mutate either experiment's original virtual fills to make the research look complete. After any Windows shutdown, gaps must be surfaced and treated as incomplete rather than reconstructed trades.

## 2026-10-22 production activation and pinned code (PR integration)

The frozen **2026-10-21 preregistration is unchanged**. The actual 2026-10-22 00:41 UTC read-only preflight showed `WAITING_FIRST_DUE`, empty issue list, 6 unique accounts, empty opening positions, no historical events, and identical protocol hash `6f5e9cf6333d3dd5fe4339ef9d7ba401df36bc361915584bf45226f464cc5054`.

Both unprivileged `freequant-clean-shadow.timer` and `freequant-clean-watchdog.timer` were **enabled 2026-10-22 09:43 KST**. Their immediate startup services succeeded. The 09:44 clean-shadow scheduled run correctly returned `OUTSIDE_PROTOCOL_WINDOW` for **decision day 2026-10-21** and created no virtual events. First legitimate evaluation decision day is **2026-10-22 UTC** and may only be recorded from **2026-10-23 09:10–10:19 KST**; first scheduled attempt **09:24 KST**. First missing-day audit after grace is **2026-10-23 10:50 KST**.

To avoid an innocent development-branch checkout changing the experiment's model code mid-flight, after GitHub CI passes the clean user services are pinned to **`/home/hong/apps/FreeQuantLab-clean-runtime`**, a detached Git worktree at the audited merge SHA. Executable, protocol, and config are loaded from that immutable detached code checkout; existing live state and alerts remain at absolute paths under `/home/hong/apps/FreeQuantLab/state/` and `/home/hong/apps/FreeQuantLab/reports/clean-daily/`. Only this cohort's two `.service` units change; existing four timer names, times and enablement remain unchanged. A future code update requires new validation and explicit re-pinning, not simply checking out a different development branch. Do not delete or copy a fresh SQLite ledger over the existing pre-registered one.

The config's `paper_min_days=90` is a **research eligibility setting**, not permission to place actual orders. The evaluation remains unblinded, read-only to real broker accounts, and ineligible for model promotion. Existing contaminated general-ledger evaluation is not repaired by the clean cohort.

Safely review when deployed:

```bash
cd /home/hong/apps/FreeQuantLab-clean-runtime
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
$PY -m quantlab.clean_cohort --db /home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db \
    --protocol /home/hong/apps/FreeQuantLab-clean-runtime/protocols/2026-10-21-btc-eth-clean-90d.json \
    --alert-db /home/hong/apps/FreeQuantLab/state/clean-watchdog.db \
    --report-dir /home/hong/apps/FreeQuantLab/reports/clean-daily
systemctl --user show freequant-clean-shadow.timer freequant-clean-watchdog.timer \
    -p Id -p ActiveState -p UnitFileState -p NextElapseUSecRealtime
journalctl --user -u freequant-clean-shadow.service -u freequant-clean-watchdog.service -n 30 --no-pager
```

All operations above are user-level; **sudo is not required**. If startup conditions are not met, do not invent missed dates, backfill paper orders, re-register any protocol, or transfer old balances to the clean cohort.

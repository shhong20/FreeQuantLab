# Forward protocol collection compliance and first-observation runbook

Date: 2026-10-20. Host: `DESKTOP-S51Q473`. Repo: `shhong20/FreeQuantLab`.

**Scope:** Monitoring and guarding the existing six BTC/ETH *virtual* strategies. No broker orders, credential management, automatic promotion, fabricated fills or retrospective repairs.

## Practical timeline (UTC and Seoul)

The 90-day preregistration at `protocols/2026-10-20-btc-eth-90d.json` contains **decision dates 2026-10-21 through 2027-01-18 UTC**.

Upbit UTC daily candles end at **09:00 KST** the following calendar day. The existing shadow timer attempts virtual next-session observations at 09:15/09:25/09:35/10:00 KST, and the separate watchdog checks at 10:30/12:30/18:30 KST. Missing protocol data are not considered overdue until **10:20 KST**.

The first eligible *protocol* decision is UTC 2026-10-21, which cannot have its post-close simulated observation until **2026-10-22 at 09:15 KST or later**. The earlier 2026-10-21 09:15 KST Shadow Paper run may record the UTC 2026-10-20 daily candle: this is genuinely forward after account registration, but **outside the registered 90-day scoring window**, and must not be silently added to its 90-day performance.

The first protocol-missing warning, if Oct 21 decision has still not been recorded, becomes due **2026-10-22 at 10:20 KST**, not Oct 21.

## Execution guard

When deployed, `freequant-shadow.service` passes:
```
--protocol protocols/2026-10-20-btc-eth-90d.json
```

**Before any new ticker, candle API request or simulated order**, the process checks:
- preregistration file exists and its exact SHA-256 content hash is valid
- current UTC time is not before the registration time
- same six track IDs, symbol, label, registered time, frozen candles, rules and settings as in the manifest
- no unexpected additional/removed strategy in the active universe

A mismatch **raises an error and aborts the cycle**. There is no auto-rewriting of the manifest, no repair of the trading database and no backfilling missing sessions. The timer may retry at its next fixed time only after the mismatch is manually resolved through a new independently reviewed plan (never by editing the frozen past).

## Read-only due-date and coverage check

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m quantlab --db state/shadow.db \
  protocol-check --protocol protocols/2026-10-20-btc-eth-90d.json
```

This command performs a ledger integrity audit and frozen-identity hash verification, and returns:
- `REGISTERED_WAITING_FIRST_DUE` prior to the first eligible UTC daily candle's close + 80-minute grace
- `COLLECTING_AS_PREDECLARED` when all **due** daily decisions appear for every registered track
- `BREACH_OR_MISSING` when any due decision is absent or its recorded time/session is inconsistent
- `INVALID_PROTOCOL_IDENTITY` or `INVALID_LEDGER` for strategy drift or ledger errors
- `WINDOW_CLOSED_PENDING_MANUAL_REVIEW` after the 90-day window ends, if all due records exist

Each strategy has `due_days`, `observed_due_days`, `missing_due_days`, `first_missing_utc_day`. **No prices, units, gains, return metrics, or interim evaluation scores** are included in this compliance endpoint. The registered window's return table remains locked until its original cutoff.

The read-only local API `GET http://127.0.0.1:8788/api/protocol-collection` displays the same counts next to the existing protocol-lock status in the dashboard.

## Watchdog integration and failure handling

`freequant-watchdog.service` now passes the same `--protocol` argument. A due-day coverage breach emits an operational warning `PROTOCOL_DUE_OBSERVATIONS_MISSING`. Incorrect account identity, corrupted ledgers, missing/tampered protocol, or a failed compliance check emit a **critical** operational alert. Existing Windows best-effort notification delivery and local SQLite persistent alert inbox continue to apply.

Reports include `protocol_collection_compliance`, but *never* replace previously missing observation dates with synthetic capital values. If a manifest is tampered with or the account identity check fails, the watchdog marks itself DEGRADED and **withholds a nominal new daily performance report**.

The alert key is stable per issue to avoid repeated notifications every 2h; the local inbox retains transitions/critical reminders. Server outages may prevent timely monitoring; restart does not rewrite history.

## Sanity commands

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m quantlab --db state/shadow.db protocol-check \
  --protocol protocols/2026-10-20-btc-eth-90d.json
.venv/bin/python -m quantlab --config examples/shadow_crypto.json \
  --db state/shadow.db shadow-cycle \
  --protocol protocols/2026-10-20-btc-eth-90d.json

export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user is-active freequant-shadow.timer freequant-dashboard.service freequant-watchdog.timer
systemctl --user list-timers --all freequant-shadow.timer freequant-watchdog.timer
journalctl --user -u freequant-shadow.service -n 40 --no-pager
journalctl --user -u freequant-watchdog.service -n 40 --no-pager
```

Manual `shadow-cycle` will **not** produce any virtual fill unless a truly new completed UTC candle exists. It cannot fabricate the next day's market data.

## Limitations

One exchange API is not source-independent market truth. Reported BTC/ETH paper fills use public ticker-based approximations and still do not model live tradable spread or partial fills. The current interim dashboard exposes ordinary paper-account performance, so the experiment remains preregistered but **not blinded**. All future alpha claims require separate, genuinely untouched evidence and a human decision.

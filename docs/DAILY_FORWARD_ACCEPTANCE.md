# Daily forward observation acceptance — 2026-10-21

This is a **read-only** software evidence checker for actual time-stamped BTC/ETH *virtual* forward paper decisions. It does not place trades, fetch external market data, train a model, replay missing trading days, or reveal locked strategy returns.

## Observed first day — 2026-10-21 10:20 KST

Actual production SQLite and systemd were inspected directly at 10:20 KST:

| Lane | UTC decision | First saved at KST | Independent accounts | Duplicate account/day | Evidence |
|---|---|---|---:|---:|---|
| Exaone | 2026-10-20 | Oct 21 09:18 | 2 (BTC, ETH) | 0 | Ledger, saved Upbit ticker |
| Qlib LightGBM | 2026-10-20 | Oct 21 09:21 | 2 (BTC, ETH) | 0 | Ledger, saved ticker and exact model feature/probability replay PASS (2/2) |
| Legacy original baseline | 2026-10-20 | Oct 21 09:15 | 6 | 0 | Accounting and quote PASS, but **not in preregistered 90-day window** |

The original baseline protocol is locked for **UTC 2026-10-21..2027-01-18**, so the six legacy events (including four paper BUYs) must never be counted as clean 90-day opening inventory. The new daily acceptance checker displays the original as `WAITING_FIRST_DUE` on Oct 21 and flags the separate research-integrity issue `PREWINDOW_REVIEW_REQUIRED`.

Actual repeated user-systemd invocations at 09:28/09:38/10:05 (Exaone) and 09:31/09:41/10:08 (Qlib) finished `Result=success` without duplicate events; original baseline subsequent attempts at 09:25/09:35/10:00 also produced no duplicates. Exact model/quote audit and SQLite uniqueness were checked directly.

## What is scheduled for Oct 22, 2026?

The original 90-day study's first in-window UTC candle (**2026-10-21**) closes at **09:00 KST on October 22**. Baseline virtual observation is scheduled at 09:15, followed by Exaone at 09:18, Qlib at 09:21 (plus existing retry windows).

All sessions become **due at 10:20 KST**. The new `freequant-daily-receipt.timer` checks **at 10:50 and 12:50 KST**, as well as ~5 minutes after WSL/user-systemd startup. Its report expects:
- 6 distinct account/date events for **original BTC/ETH** at UTC decision date 2026-10-21
- 2 distinct Exaone account/date events at UTC decision date 2026-10-21
- 2 distinct Qlib account/date events, plus exact frozen model replay, at UTC decision date 2026-10-21
- one immutable public quote per BTC/ETH market and lane/day, matching all recorded virtual events
- original UTC-session timestamps within the approved 09:10–10:19 KST observation window
- virtual accounting/assumed costs, public-quote evidence and 800-day historical BTC/ETH archive audits PASS

If a required event or its price evidence is absent, reports show `INCOMPLETE` and the service exits nonzero rather than fabricating an event. Already-stored earlier dates cannot be rewritten by the checker. If a machine was off, recoverable historic OHLCV lives in `state/market_history.db` but the missed hypothetical opening ticker/fill is **not backfilled**.

The legacy original pre-window BUY risk remains `research_integrity_status=PREWINDOW_REVIEW_REQUIRED` even if all ten accounts collect today's eligible UTC candle; it is a separately disclosed research-design breach, not an operational ledger failure.

## Operator commands

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m quantlab.daily_receipt

# New scheduled read-only receipt
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user status freequant-daily-receipt.timer
systemctl --user list-timers --all 'freequant*'
journalctl --user -u freequant-daily-receipt.service -n 60 --no-pager

# Local immutable daily evidence reports:
cat reports/daily-receipts/2026-10-21.json
curl -fsS http://127.0.0.1:8788/api/daily-receipt
```

The daily snapshot is `reports/daily-receipts/YYYY-MM-DD.json` (KST local date), atomically overwritten by a new read-only audit on repeated checks, permission 0600. The local-only dashboard endpoint refuses to expose locked performance fields and rejects snapshots more than 28 hours old. `daily_receipt.inspect_receipt` is purely observational and never invokes forward virtual-paper engines or external brokerage/network requests. The report says `original_90d_returns=null` until further manually audited research.

## Limitations

- Virtual paper BUY/SELL/HOLD records are **not real order executions**; public Upbit last-trade ticker is not a verified executable bid/ask.
- Verifying all required daily event records and model replay does not imply the strategy is profitable or statistically significant.
- Keeping a Windows/WSL machine online is necessary for timed checks; a Windows shutdown suppresses even systemd `Persistent=true` until WSL starts again.
- No automatic model promotion, live brokerage access, strategy retraining, or synthetic reconstruction of missed trades.

# Immutable Upbit historical market recovery after Windows/WSL restart

**FreeQuantLab · 2026-10-21 KST · public market data only**

## Actual recovery performed today

The Upbit public day-candle API returned **800 fully completed consecutive UTC daily candles each** for KRW-BTC and KRW-ETH, from **2024-08-12 through 2026-10-20 UTC**. These **1,600 real publicly served OHLCV observations** were inserted into `state/market_history.db`. The read-only archive audit returned `PASS`.

The preexisting forward virtual accounts were **not** altered by this market-data import. Their hypothetical quote/fill records may only be recorded *at an actual valid future observation time*, never invented from a later daily OHLCV close.

## How catch-up after downtime works

`quantlab.market_archive` provides an independent, file-locked SQLite archive and never opens any of the three virtual account ledgers for writes.

- On first run: fetch and validate an 800-day overlap (Upbit public UTC daily candles, not mock), then seed the immutable archive with actual retrieval timestamps and `historical_seed` provenance.
- After returning from sleep/reboot: fetch 800 fully closed candles again, verify all overlapping previously archived prices and volume, and append **only the missing calendar dates**. Newly caught-up rows get `historical_catchup` provenance and the **actual recovery time**, not a fabricated original trade or collector timestamp.
- 0 missing dates -> 0 appended; repeated calls are idempotent.
- Exchange data changed for any overlapping date -> fail closed, **do not rewrite older values**; explicit provider/manual review needed.
- A provider omission or an offline gap that exceeds the available 800-day overlap -> fail closed, preserve the archive. Do not fill any missing UTC days with fabricated candles.
- If BTC fails but ETH is intact, ETH can catch up independently; the failed market's rows remain intact until a healthy retry.
- UTC timestamps, OHLC bounds, consecutive daily sessions, nonzero recent volume and extreme daily price ratios are validated using the existing strict Upbit checks.

The full **past OHLCV archive** can now be recovered after a short or medium outage. *It cannot reconstruct the original public last-traded ticker at 09:18/09:21 KST or the exact simulated buy/hold/cash equity produced at that instant.* Filling those in the original prospective account DB would falsely claim an observation that never happened.

## Automatic scheduling

New standalone units, without changing the existing five auto-paper/watchdog timers:

- `freequant-market-archive.timer`: 09:07 / 09:47 / 12:07 KST every day. `Persistent=true` invokes the last missed daily refresh after WSL starts.
- `OnStartupSec=2min`: invoke catch-up after a WSL user-systemd manager starts; if a daily scheduled activation is missed during downtime, the archive still synchronizes on boot.
- `freequant-market-archive.service`: one-shot public-data-only download, 180-second timeout, `ProtectSystem=strict`, `ReadWritePaths` limited to `state/`, `NoNewPrivileges=true`, `UMask=0077`. No broker/API secrets or paid market feed.
- Any failure returns nonzero and is also detected by the independent watchdog as `MARKET_HISTORY_CATCHUP_REQUIRED` (separate durable alert history). Read-only localhost endpoint `GET /api/market-archive` shows archive rows, latest UTC day and unresolved issues.

**Important:** systemd only runs while its WSL Linux distribution is online. Linger may keep the user unit manager running, but **does not power on a shutdown Windows machine**. A Windows logon startup task or a continuously-on device is needed to start WSL after a full Windows shutdown. This archive's timer will safely catch up from the historical API once WSL does start.

## Existing 90-day paper evaluation disclosure

At ~09:15 KST on 2026-10-21, the original 6-account paper runner recorded **2026-10-20 UTC virtual events** (including 4 BUY actions), one day **before** its frozen 90-day formal start of **2026-10-21 UTC**. These are *real-time virtual observations*, not real exchange orders, but they fall outside the registered evaluation period. They were NOT created by the OHLCV archive. **Do not delete, move, or silently count them as formal 90-day results.** Before claiming confirmatory 90-day performance the experiment's opening inventory/fees need separate documented treatment or a clean re-registration; this code does not fix accounting retroactively.

The supplemental Exaone and Qlib lanes were explicitly authorized to start their virtual observation on the **2026-10-20 UTC completed bar**; those are different experimental registration windows.

## CLI verification

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m quantlab.market_archive catch-up
.venv/bin/python -m quantlab.market_archive status
systemctl --user list-timers --all 'freequant*' --no-pager
systemctl --user status freequant-market-archive.timer --no-pager
journalctl --user -u freequant-market-archive.service -n 55 --no-pager
curl -fsS http://127.0.0.1:8788/api/market-archive
```

If a malformed/revised Upbit candle is detected, review the source and the immutable archive in read-only mode; do not erase the database or rewrite frozen evaluation records.

## Tests

Regression tests simulate three missed UTC days with real-looking **synthetic test fixture OHLCV** only, idempotent re-fetch, revised exchange source data, offline gap beyond available provider history, a BTC-only API failure while ETH succeeds, duplicate dates, invalid candle bounds, and invalid virtual ledger paths. None of these test fills are part of actual market performance.

# Frozen 90-day pre-window virtual exposure incident and strict evaluation exclusion

**Observed on 2026-10-21 KST**. Research-only. Do not silently erase, relabel, or recalculate existing virtual fills.

## Incident established from actual WSL records

The original SHA-256-locked 90-day protocol is `protocols/2026-10-20-btc-eth-90d.json`. Its immutable scoring window is **2026-10-21 through 2027-01-18 UTC** (90 days), not before, and the evaluation gate remains **2027-01-19T00:00:00Z**.

On **2026-10-21 at ~09:15 KST**, the original auto-paper runner saved **6 actually observed hypothetical events for the completed 2026-10-20 UTC daily bar**, one day *before* the formal scoring window. **4 of those were virtual BUYs**, across the baseline BTC/ETH buy-and-hold and MA trend accounts; the other two factor accounts were HOLD. These are paper-only observations from actual public Upbit tickers at the time, **not broker fills**. They were NOT synthesized by the separate historical OHLCV recovery project.

The saved virtual ledger itself passed normal accounting and quote consistency audits. But cash/position/fee state now starts the formal 90-day period already exposed to a prior-day virtual fill. Comparing these accounts as if all began flat at exactly 2026-10-21 would be false. This is a protocol-design/runner-timing breach, not evidence of positive or negative investment alpha.

## Prevent recurrence without tampering with original evidence

- The baseline `shadow.run_cycle` now checks the **exact original hash-validated evaluation first/last UTC dates BEFORE any public market request or virtual position mutation** when `protocol_path` is used. A completed bar outside that window returns `OUTSIDE_PROTOCOL_WINDOW`, zero simulated fills and no ticker lookup, and writes only an explicit `shadow_runs` skip audit entry. This does NOT rewrite any existing premature positions or any model settings.
- `quantlab.protocol_compliance.assess_collection` reads existing events before the frozen start and returns `BREACH_OR_MISSING` with `PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY`, the first unexpected day, total out-of-window event count and out-of-window BUY/SELL count. It exposes **no equity or return**.
- `quantlab.prospective.inspect_protocol` keeps 90-day outcomes locked; before the 2027 cutoff it returns `LOCKED_PRE_WINDOW_ACTIVITY_REVIEW`, counts and `opening_inventory_requires_manual_review=true`. Even after the cutoff it labels the original experiment `PREWINDOW_VIRTUAL_EXPOSURE_REVIEW` (not confirmatory validation).
- The independent existing 90-day Watchdog emits a durable `PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY` alert distinct from missing-day warnings. It does not report a false missing day before the first due UTC candle.
- `quantlab.lane_comparison` refuses cross-lane 90-day virtual performance with `INTEGRITY_REVIEW_REQUIRED` and `FROZEN_90D_PRE_WINDOW_EXPOSURE_REVIEW_REQUIRED`. No three-lane scores can be shown automatically, even after the cutoff. No strategy promotion or broker order is enabled.

## Manual future research decision (NOT performed automatically)

The legacy six-account cohort must be preserved for auditable **exploratory** observations. Before claiming a clean *future* 90-day experiment, a researcher would need to make a **new explicitly registered cohort and a new future-only protocol** (new distinct DB and frozen IDs, starting flat at a properly predetermined UTC day). The old cohort is not deleted, adjusted or reset; no historical pretend trades or missing-day fills are generated.

A manually justified descriptive analysis of the already-open-account cohort might be possible, but is **not** a preregistered flat-opening experiment and cannot be compared as if it were. Frozen candidates may exhibit selection effects, and public last-trade hypothetical fills differ from executable orderbook fills.

## Proof

- Real WSL audit: `PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY` with **6 events, 4 virtual orders, first day 2026-10-20 UTC**, zero due dates yet at the observation time.
- Synthetic offline regression inserts exactly such a legacy premature simulated trade, then confirms audit warnings and untouched SQLite rows, locked return values, Watchdog alert, and refusal by cross-lane comparator before and after the formal cutoff.
- An independent regression proves that the new frozen-protocol-aware runner skips the bar before `first_eligible_decision_utc`, records a skip in `shadow_runs` and neither fetches market prices nor inserts virtual events.

Monitoring commands:

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m quantlab --db state/shadow.db protocol-check \
  --protocol protocols/2026-10-20-btc-eth-90d.json
.venv/bin/python -m quantlab.lane_comparison
curl -fsS http://127.0.0.1:8788/api/protocol-collection
```

The original 6 accounts and the separately authorized Exaone/Qlib lanes are deliberately not mutated by these checks. The historical `state/market_history.db` recovers only Upbit exchange market candles, not simulated fills.

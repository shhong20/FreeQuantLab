# Stock daily evidence: remaining 9 price-conflict gaps (2026-11-03 KST)

## Operational incident
Immutable source observation ledger and long independent vendor history now reach 2026-11-02 for all 6 tracked stocks, following PR #121's source-verified volume-only catch-up. However, after matching `daily_bars` to `long_bars` from 2026-10-20 onward, **9 observed market/session pairs still lack a long-history counterpart**. Previous material OHLC revisions remain quarantined, not forced into backtests.

## Independent reproducible evidence
Module `quantlab.stock_gap_provenance` opens both stock-only SQLite databases **read-only**, verifies the reference-market/ticker/day, original observed OHLCV, every conflicting vendor raw payload's independent SHA-256, vendor response decoded to the particular missing session and values saved in immutable conflict entries. No source or position DB is written.

The persisted audit from the actual WSL data:
`state/stock_daily/quality_reports/gap_provenance_20261103_r1.json` + `.sha256`.
Both report files have 0600 permissions. All 9 actual gaps have verified raw provenance.

| Symbol | Dates missing | Recent vendor price conflict classification |
|---|---|---|
| 069500 KODEX 200 | Oct 22/26/27/28 | COHERENT_OHLC_SCALE_SHIFT_POSSIBLE_ADJUSTMENT_NOT_VERIFIED (4) |
| AAPL | Oct 22/27 | SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED (2) |
| MSFT | Oct 22 | MATERIAL_OHLC_PRICE_CONFLICT (1) |
| MSFT | Oct 27 | SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED (1) |
| SPY | Oct 27 | SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED (1) |

KODEX 200's latest provider source OHLC ratios are about 0.99857 relative to its immutable first-observed prices (approximately -14.2 to -14.3bps), consistent across each of the four price fields. **Hypothesis only**, not proof of corporate action, ETF adjustment or correct executable prices. Its October 22 earlier raw snapshot agrees with first-observed OHLC, but latest vendor prices subsequently diverged. Do not silently choose older or newer data as the trading truth.

AAPL's open differs by approximately $0.005, and SPY's high by approximately $0.01 on disputed dates. MSFT Oct 22 has an approximately $0.44 open difference (about 8.26bps) and remains explicitly material. No percent-based or absolute-tick tolerance has been applied to overwrite primary history.

## Reproduction
```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
/home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.stock_gap_provenance \
    --since 2026-10-20 \
    --report /home/hong/apps/FreeQuantLab/state/stock_daily/quality_reports/NEW_UNIQUE_REPORT.json
```

- `--report` requires a new path and produces a 0600 SHA-256 pinned immutable JSON. Never overwrite.
- Every reported conflict SHA and source bytes are independently checked. Missing/incorrect raw rejects the audit. Source price reported under a conflicting snapshot must equal the corresponding raw market date.
- The report intentionally records `paper_or_live_trading_eligible=false`, `confirmed_historical_strategy_returns=null`, `broker_orders=0`.
- Return code 0 means read-only provenance **audit code executed**, not that price conflicts were resolved. Report `status=VENDOR_HISTORY_GAP_PROVENANCE_UNRESOLVED_NO_TRADING` remains mandatory.

## Forward 90-day evaluation
The clean crypto 90-day registration has 66 due account-days as of 2026-11-03 09:26 KST and 24 actual observations. 42 missing account-days are not backfilled as past executed paper trades; an intact ledger and quote SHA do not make the experimental protocol complete. Its strategy return remains unapproved. This stock-only provenance tool does not touch the existing 90-day cohort.

## Next independent data verification
Investigate Naver 069500 possible adjusted price factor against issuer/exchange corporate action evidence and check Yahoo US split/dividend/precision treatment against historical trade reference. Only after independent source proof should a new *explicitly versioned*, non-destructive research-normalization dataset be built; never silently rewrite the frozen initial observations.

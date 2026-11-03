# 2026-11-03 — Source-pinned stock price-basis arithmetic sensitivity

## What this diagnoses
This is **not** a trading strategy backtest, equity return, fill simulator or authorization to use the disputed bars. It is a deterministic arithmetic audit of how open/close movement, daily high/low range and price levels differ when choosing different observed OHLC variants. It intentionally does not carry any strategy's entry, exit, sizing, slippage or brokerage details.

Independent immutable parents (SHA pinned, rechecked directly against both original read-only SQLite stock databases):
- `quality_reports/gap_provenance_20261103_r2.json(.sha256)`
- `quality_reports/us_public_two_source_20261103_r1/` — 6 public Yahoo Finance / Stock Analysis HTML captures; 5 unresolved AAPL/MSFT/SPY stock sessions.
- `quality_reports/etf_distribution_study_20261103_r1/` — KODEX 200 issuer 136 KRW dividend hypothesis.
- `research_variants/kodex200_distribution_v1_20261103/` — 4 ETF days with three explicitly non-canonical price variants.

The independent third price source is [QUODD / Zacks MSFT price history](https://quodd.zacks.com/pricehistory/pricehistory.php?t=MSFT), read without credentials or orders from a fixed HTTPS URL. The 2026-10-22 vendor-published EOD values are **O 533.140, H 533.596, L 520.300, C 521.080 USD**, volume 21,266,610. We save exact returned raw HTML and original SHA-256, parse the source's exact MSFT/date table, and reparse on each verification. The third party **does not certify a Nasdaq auction price or raw execution tape**.

Critical independent precision observation: third source MSFT **open matches the later vendor** (533.14); its **high matches the initial observed source** (533.596), whereas the later vendor has 533.60. Low and close match both. Therefore no one WHOLE historical daily bar should be assumed canonical merely because the open/close agrees with one data provider. Nasdaq's public official historical page did not return an independently verifiable dated historical row in this investigation; its Nasdaq Data Link raw daily source can require an API key. Do not claim exchange official proof.

## Actual numerical comparison of original vs later-vendor prices

| Symbol and session | Later minus initial open (USD) | Open basis delta (bp) | Difference in arithmetic same-day C/O movement (bp) |
|---|---:|---:|---:|
| AAPL 2026-10-22 | +0.005005 | +0.145096 | −0.146476 |
| AAPL 2026-10-27 | +0.005005 | +0.145845 | −0.147515 |
| MSFT 2026-10-22 | +0.440002 | **+8.259854** | **−8.073011** |
| MSFT 2026-10-27 | 0 | 0 | 0 |
| SPY 2026-10-27 | 0 | 0 | 0 |

Math:
- Open basis delta bp = `10000 * (later_open/original_open - 1)`.
- Same-bar open→close arithmetic movement bp = `10000 * (close/open - 1)`.
- The comparison of two such movements is a pure price sensitivity result, **not a realized or simulated strategy return**, and does not show order feasibility at that opening price.
- For SPY on 2026-10-27 the **high** differs by about USD 0.01 (range diagnostic ≈0.1304bp); the open and close are unchanged.
- AAPL first observed open is near a cent-display rounding tie: report it separately from MSFT's clear material price difference.

KODEX 200 2026-10-22/26/27/28:
- Hypothesized issuer cash-dividend factor `(95650-136)/95650=0.9985781495` shifts comparable price level by **−14.2185bp** per day when applied uniformly to all OHLC.
- Theoretical same-day C/O arithmetic percentage is unchanged under uniform positive scaling (0bp up to numerical precision).
- **Never splice price bases across dates**: a single ex-dividend boundary with mixed adjusted/unadjusted pricing can induce a spurious artificial overnight jump.
- Official issuer 136 KRW cash distribution is verified; this algebraic factor **is not vendor methodology certification** or independently validated Nasdaq/KRX raw execution data.

## Saved evidence

Research-only WSL path:
```text
/home/hong/apps/FreeQuantLab/state/stock_daily/quality_reports/
  stock_price_basis_sensitivity_20261103_r1/
    quodd_msft_20261022.html
    report.json
    report.sha256
    manifest.json
    manifest.sha256
```

All files 0600; directory 0700. The report includes five US-bar source comparisons, four ETF price-basis comparisons, source hashes and explicit flags `broker_orders=0`, `strategy_pnl=null`, `strategy_sharpe=null`, `backtest_or_live_approved=false`. All source files, two original DBs and two research datasets remain unchanged.

Independent offline verification from the merged WSL research checkout:

```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
P=/home/hong/apps/FreeQuantLab/.venv/bin/python
R=/home/hong/apps/FreeQuantLab/state/stock_daily/quality_reports
$P -m quantlab.stock_price_basis_sensitivity --verify-only \
 --root "$R/stock_price_basis_sensitivity_20261103_r1" \
 --us-root "$R/us_public_two_source_20261103_r1" \
 --etf-root /home/hong/apps/FreeQuantLab/state/stock_daily/research_variants/kodex200_distribution_v1_20261103 \
 --study-dir "$R/etf_distribution_study_20261103_r1" \
 --gap-report "$R/gap_provenance_20261103_r2.json"
```

The program rejects invalid raw vendor SHA provenance, tampered price vectors, changes to hypothesized dividend arithmetic, rehashed fake trading approval, forged third-party HTML, inconsistent OHLC range, and rerun attempts that would overwrite immutable output. It never touches brokerage APIs, the fixed 90-day experimental cohort, or attempts to backfill expired shadow trades.

## Next gate
For any actual historical strategy sensitivity **only after independent price-basis standardization**, use completely separate research cohorts, explicit model train/test dates and a frozen execution-cost model. Until a verifiable exchange/official event and adequate raw price source are available, do not claim Sharpe, forward return or outperformance.

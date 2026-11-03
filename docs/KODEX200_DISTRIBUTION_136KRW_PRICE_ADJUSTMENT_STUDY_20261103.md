# KODEX 200 ETF: issuer 136 KRW cash distribution vs missing historical OHLC (2026-11-03 KST)

## First-party issuer evidence
- 삼성자산운용 2026-10-27 분배금 공지: `KODEX 200 ETF`, code `069500`, **136 KRW per unit**: https://www.samsungfund.com/etf/lounge/notice-view.do?no=80553
- The issuer's official product page documents **January, April, July and October final business-day** distribution-record conventions: https://www.samsungfund.com/etf/product/view.do?id=2ETF01
- For the Oct-2026 study, reference unadjusted 2026-10-28 close comes from immutable first-observed stock daily evidence: **95,650 KRW**. The date of prospective ex-distribution (2026-10-29) is *inferred for the study*, not separately certified by primary-source trading-day event feed.
- A cash distribution does **not**, by itself, prove the adjustments of any vendor's historical OHLC.

## Calculation using a fixed distribution amount
Reference-based **hypothesis only**:
```text
hypothetical_factor = (pre-ex observed close - issuer payout) / pre-ex observed close
                    = (95,650 KRW - 136 KRW) / 95,650 KRW
                    = 0.9985781495033978

hypothetical_earlier_OHLC = original_first_observed_OHLC * hypothetical_factor
residual = latest_unofficial_vendor_OHLC - hypothetical_earlier_OHLC
```
The same **predeclared** reference and issuer payout explain all original-vs-vendor 16 OHLC observations from **2026-10-22, 26, 27, 28** to within **1 KRW maximum absolute residual**. No factor is fitted to vendor prices; the calculation uses the first-observed Oct-28 close and issuer distribution amount only. This is stronger evidence for an ex-distribution adjusted vendor-history convention than arbitrary 0.14% price drift, but still **does not prove** the vendor's internal factor method, correct historical order price, or venue-grade prices.

Data access: `quantlab.stock_etf_distribution_study` must first SHA-verify the immutable gap report, independently regenerate the gap report from two READ-ONLY SQLite source DBs, and compare exact content before applying this arithmetic hypothesis.

The sidecar records:
- Official announcement date, canonical issuer URL, and public payout amount.
- All 4 missing sessions, 16 original OHLC, 16 latest vendor OHLC, model-predicted values and residuals; individual source raw SHA and previous source-agreement flag.
- Five US unresolved source discrepancies (AAPL 2, MSFT 2, SPY 1) which **do not** inherit any Korean ETF dividend adjustment.
- Unchanged `long_bars`, `daily_bars`, virtual broker ledgers, real orders 0.
- `etf_vendor_algorithm_independently_confirmed=false`, `etf_historical_executable_price_certified=false`, `paper_or_live_trading_eligible=false`, `certified_strategy_returns=null`.

## US share-price observations
The five observed vendor gaps are independent source precision/data disagreements, not evidence of ETF distributions:
- AAPL 2026-10-22/27: small open price difference approximately USD $0.005 per share, plus changed reported volume.
- MSFT 2026-10-22: **material ~USD $0.44 open difference**. Do **NOT** round away. MSFT 2026-10-27: small low price difference.
- SPY 2026-10-27: ~USD $0.01 high price difference.
Each remains quarantined until an independent exchange/official time-of-sale EOD price feed establishes the historical print and whether a source correction was subsequently issued. A floating point representation alone is not enough to explain all discrepancies.

## Actual study artifacts
Run from an isolated merged research checkout; source and output folders stay private:
```bash
P=/home/hong/apps/FreeQuantLab/.venv/bin/python
R=/home/hong/apps/FreeQuantLab/state/stock_daily/quality_reports
cd /home/hong/apps/FreeQuantLab-etf-distribution-20261103
$P -m quantlab.stock_etf_distribution_study \
  --gap-report "$R/gap_provenance_20261103_r2.json" \
  --output-dir "$R/etf_distribution_study_20261103_r1"
```
`study.json` + `study.sha256` were generated 2026-11-03 KST with 0600 file and 0700 directory permissions. The output path is immutable; trying to overwrite fails. Source DBs remain untouched.

## Decision
**Strongly explained by issuer payout, NOT certified for backtest or trading.** A separate non-destructive, versioned reference-price series could be considered only after independent data-license and source adjustment-convention checks. Do not repair baseline records by reversing this factor or approve any order/fill based on this study.

For the 90-day experimental cohort, all original missed paper decision days remain absent. Past data recovery must not be rewritten into fictitious prospective orders.

# ETF price-quality gate & continuous 4-coin shared-cash portfolio — 2026-10-21

## Executive decision

**Research system developed; no investable AI alpha found; ETFs are BLOCKED from total-return comparison.** Earlier 20-fold geometric return numbers were not a continuously held common account. This study uses actual units + shared KRW cash with a single initial KRW 1,000,000 and no fold-by-fold capital reset. It marks each next open, executes signals at the next open, charges realized per-side transaction costs, trims exposure when drift breaches caps, and liquidates **only once at the final observation**. All transactions are hypothetical next-open executions, not real broker fills.

### ETF price / distribution / calendar audit

- Raw ETF source: unofficial Naver historical OHLCV, six-asset snapshot manifest signed by SHA-256. Source/targets 069500/114800/132030 have **identical 2,999 saved KRX dates** spanning 2014-07-28–2026-10-20; raw input hashes match.
- Independent installed `exchange_calendars` (XKRX) currently expects two additional dates, **2026-06-03** and **2026-07-17**, that all three ETFs rightly omit. 6/3 is Korean local elections; 7/17 is the restored Constitution Day holiday. These are **CALENDAR LIBRARY VERSION DIFFERENCES**, not implied missing ETF trades; never interpolate dummy price days.
- Official KODEX product pages distinguish market price performance from tax-before/distribution-reinvested NAV returns. Specifically 069500 lists cash distribution schedules; raw price OHLCV alone does not establish total-return history. This study does **not** reconstruct distributions from ex-date price drops.
- Absent verified original KRX exchange-day calendar snapshots, issuer distributions (record, ex, payment dates and payment amount), price-adjustment factors/splits, inverse rebalance and gold-fund basis/synthetic handling, ETF statuses remain `BLOCKED_TOTAL_RETURN_VERIFICATION`; **no ETF portfolio return is published** as investable evidence.
- Audit code: `quantlab/etf_price_audit.py`. Stored result: `reports/etf-price-audit-20261021.json`, excluded from Git.

Public references to corroborate policy and 2026 closures:
- https://www.samsungfund.com/etf/product/view.do?id=2ETF01
- https://www.samsungfund.com/etf/product/view.do?id=2ETFI3
- https://overseas.mofa.go.kr/vn-danang-ko/brd/m_22965/view.do?seq=309
- https://atsview.com/calendar/2026

### One shared cash account: BTC / ETH / XRP / ADA, 24h UTC data only

- Original Upbit CSV from frozen 2026-10-20 snapshots: 3,000 fully closed UTC daily bars per asset (2018-08-04–2026-10-20). SHA-256 for BTC/XRP/ADA from cross-market frozen snapshot; ETH from separate BTC-ETH historical frozen manifest. All four **calendar dates match**. No network data is queried during valuation.
- OOS signal decisions: UTC **2020-03-26–2026-10-18**, next-open fill, final mark/liquidation at UTC **2026-10-20 open**, for **2,398 consecutive daily decisions**.
- Shared start: KRW 1,000,000, fully funded no borrowing/no shorts, initial equal slice **10% target per asset** (40% total). A new asset entry is restricted by actual remaining cash, the 10% entry target and 40% basket cap. Any holding can grow beyond its original entry weight as price changes.
- To maintain equivalent *post-fill* risk discipline across strategies, excess single-name exposure above 20%, or aggregate exposure above 40%, is **sold proportionally at next open with costs**. This reduces risk but is NOT a monthly rebalancing to equal weights, and may create additional orders. An overnight rally can temporarily exceed those limits *before the next executable open*. Per-asset fractional crypto units are assumed, not tradable tick/size constraints.
- All strategy decisions are computed at the previous completed daily close. There is no use of next-open prices in model features or selection. No artificial fold-end liquidation and no reinvestment with a new account. Research model retraining every 120 days retains existing units until its signal changes.
- Strategies: `equal_weight_buy_hold` (buy all four then **risk-cap trims**; NOT classic unrestricted buy/hold), fixed `ma20_80_continuous`, and `qlib_causal_stitched`.
- Actual Microsoft Qlib LGBModel: **80 time-ordered fits** (20 for each of four assets), BTC/ETH trained on own past matured labels, XRP/ADA trained using BTC's matured labels only. Probability p>=0.55 fixed. No model or threshold selection based on OOS outcomes.
- Costs per side **15/30/60bp** charged on actual hypothetical trade notional, including risk trims and final liquidation. Model excludes executable market depth, partial fills, funding, deposits/withdrawals, transfers, delisting and tax.
- Price-only crypto spot does not require ETF cash distribution reconstruction, yet past transaction assumptions are still approximate; this study remains exploratory and no new clean future holdout exists.

### Actual continuous-account backtest (four-asset shared capital)

| Strategy | 15bp return | 15bp max drawdown | 15bp Sharpe | 15bp orders | 60bp return |
|---|---:|---:|---:|---:|---:|
| Risk-capped 4-coin held positions | +234.34% | -30.52% | 1.019 | 386 | +232.09% |
| Risk-capped MA20/80 | +205.58% | -22.69% | 1.077 | 657 | +187.73% |
| Causal Qlib stitched signals | +50.92% | -11.39% | 0.800 | 1,500 | -23.12% |

These are hypothetical results for ONE continuous common KRW account. 15bp Qlib fees/slippage model sums to **about KRW 282,789** for 1,500 simulated orders, compared with **KRW 4,720** for the risk-capped benchmark and **KRW 49,329** for MA. Total sum of per-trade fee amounts is not simply final-return difference because position timings and lost future growth differ.

All portfolios limit risk to <=40% *at post-fill sampled opens*. The benchmark was occasionally **44.11% gross just before scheduled trims** due to overnight appreciation. Therefore a hypothetical loss between closes/opens could breach intended limits; do not claim an absolute 40% real-time cap. MA also sometimes exceeds it before trimming. The lower Qlib max drawdown reflects much lower/up-and-down asset exposure as well as trading policy, **not proof of better alpha or comparable risk-adjusted forecasting**.

At 15bp, Qlib final cash ≈ **KRW 1,509,217**, 30bp ≈ 1,204,672, and 60bp ≈ 768,830. These cannot be used for real capital forecasts. The 60bp scenario loses principal despite the long evaluation span.

### Verification and red lines

- 245 Python regression tests passed after developing the new engine. Included: deterministic flat-price cash conservation; actual cost conservation; next-open causal signal shift; no fold reset; OOS mutations do not rewrite past equity; floating units and final liquidation; risk trims/rebalance overhead; 4-asset date alignment; unknown equity symbol rejection; corrupted ETF hash failure; source-only Qlib smoke refits.
- The ETF audit explicitly reports `total_return_performance_publishable=false`; unverified corporate-action and distribution evidence cannot be silently interpreted as zero payout.
- Research report results live only in ignored `reports/`, model artifacts in ignored `state/`. No edits to original paper trading ledgers or configured systemd timers. **Real orders / automatic strategy promotion remain false**.
- No inference of stable or independently verified profit: historical OOS dates have been repeatedly examined in earlier strategy research.

### Reproduce, no network

```bash
cd /home/hong/apps/FreeQuantLab-continuous-20261021
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m pytest -q
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.continuous_portfolio \
 --cross-snapshot /home/hong/apps/FreeQuantLab-crossmarket-20261021/reports/cross-market-snapshot-20261021 \
 --historical-snapshot /home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021 \
 --output-dir reports/new-unique-continuous
```

ETF audit call:

```bash
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -c "from quantlab.etf_price_audit import audit_etf_snapshot; print(audit_etf_snapshot('/home/hong/apps/FreeQuantLab-crossmarket-20261021/reports/cross-market-snapshot-20261021')['status'])"
```

Next development should obtain **issuer-confirmed ETF cash distributions and adjustment-event feeds** before any ETF total-return valuation, then perform an independently registered *future* evaluation once historical exploratory strategy design is frozen.

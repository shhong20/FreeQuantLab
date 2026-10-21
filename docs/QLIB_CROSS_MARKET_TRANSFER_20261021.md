# Cross-market Qlib transfer study — 2026-10-21

## Research contract and interpretation

**Historical exploratory transfer only. No validated alpha, broker orders, live model updates or automatic promotion.** Models are re-trained with Qlib `LGBModel` on source-asset **matured past labels only**, then applied to a distinct asset without using that target's outcome labels for fit, early stopping, threshold selection or feature screening. This is a geographic/asset transfer study, **not** a new uncontaminated chronological holdout. Crypto targets correlate with BTC; the KRX ETF data are public historical bars and may have unverified corporate-action/derivative-rolling adjustments.

Model architecture, nine feature names, fixed `p>=0.55`, fixed long/cash (20% maximum gross exposure), next-open execution, forced end-of-fold exit, and 15/30/60bp **per-side** fees/slippage all match earlier research. Secondary non-AI references: 20% buy/hold and fixed MA20/80. No candidate is chosen based on its performance.

### Frozen sources and calendars

| Universe | Train source | Evaluation targets | Sessions | OOS interval |
|---|---|---|---|---|
| KRW crypto spot | KRW-BTC | KRW-XRP; KRW-ADA | 3,000 complete UTC daily sessions; seven-day calendar | UTC 2020-03-26–2026-10-18; 2,398 days per target |
| Korean ETF | 069500 (KODEX 200) | 114800 (index inverse ETF); 132030 (gold-linked ETF) | 2,999 complete KRX sessions; exchange holidays retained as gaps, no fake bars | KRX 2017-01-03–2026-10-16; 2,397 sessions per target |

KRX ETF names are illustrative identifiers; **verify legal fund names, tracking, inverse rebalancing, distributions, NAV adjustments and any splits using an authoritative fund data feed before economic interpretation**. ETF 114800 is a daily inverse exposure, so a long-term held inverse ETF is structurally a weak comparator in some markets; its loss reduction is NOT evidence of positive or tradable alpha.

- Data source: Upbit public UTC daily candles; Naver unofficial daily KRX ETF historical data (source does not guarantee audited adjusted OHLCV).
- `reports/cross-market-snapshot-20261021/manifest.json` contains per-symbol CSV SHA-256, range, row count, retrieval time; acquisition is one-time only, evaluation refuses altered input or a nonempty output directory.
- Each source/target in a family must have **identical session dates**; no resampling, interpolation, missing calendar fabrication or cross crypto/stock alignment. KRX holidays can cause gaps of up to 14 calendar days; longer gaps are refused, but the holiday validation is not an official exchange trading-day audit.
- Starting with 600 source training sessions, the model is **refit every 120 sessions** with internal matured-label training/validation. Folds are nonoverlapping; last fold may be shorter. Test outcomes are used only for scoring, never for selecting a policy.
- These are 2 families × 2 targets × 20 folds = **80 actual Qlib fits** and 4 × 20 × 3 cost levels × 3 strategies = **720 strategy-cost-fold observations**, NOT independent research trials.

### Observed results, 15bp/side (20%-exposure, independent-fold cumulative hypothetical returns)

| Evaluation asset | Same-asset buy/hold | MA20/80 | Qlib source-only transfer | Qlib simulated orders |
|---|---:|---:|---:|---:|
| KRW-XRP | +133.94% | +35.82% | +38.43% | 344 |
| KRW-ADA | +124.79% | +139.46% | +43.52% | 408 |
| 114800 inverse ETF | -27.81% | -0.96% | -5.99% | 208 |
| 132030 gold-linked ETF | +22.65% | +13.99% | -4.00% | 274 |

**No positive, credible generalizable Qlib alpha detected.** BTC→XRP/ADA misses strong rising-market participation. KRX 069500→114800 produces a less-negative simulated return than the buy/hold negative benchmark, but Qlib still loses money (-5.99%), the target has mechanically inverse exposure and its adjusted-quote/rolling quality has not been audited. In the gold-linked ETF, Qlib underperforms significantly. This cannot be used for strategy promotion.

At 60bp per-side, Qlib simulated return is **XRP +1.55%, ADA -0.62%, inverse ETF -22.05%, gold-linked ETF -24.99%**, again hypothetical. MA20/80 on ADA has an apparently better historical result, but exploratory corrected maxT for this single two-candidate family is about 0.55, not evidence of stable alpha. For 114800, even if a preliminary maxT within the two fixed candidates appears small, it **does not correct** for multiple preceding BTC/ETH/horizon/feature experiments or the biased inverse benchmark.

The method is not truly a cross-market *trained* single model: the crypto universe has its own BTC model, and the KRX universe has its own 069500 model. No crypto-to-equity model is claimed. Date calendars and currency effects differ, so cumulative figures from the two universes must not be directly ranked.

### Controls and reproducibility

- Causal label: decision at closing session t → source asset open[t+1] to open[t+2]. Fit cutoff excludes labels whose t+2 open was not yet observable. Target outcome labels are held out completely.
- The same nine features and model hyperparameters as existing Qlib are used on every source. Target asset's closing OHLCV, but **not future target closes**, produce target features and predictions.
- Regression tests: duplicate/missing crypto candles, date alignment mismatch, KRX holiday gap policy, corrupted CSV SHA, exact source-training-only proof, actual Qlib future source price mutation invariance, fixed candidate/cost/fold paths.
- Both public-data acquisition and evaluation only write to new ignored `reports/` and `state/` research locations. Existing operational ledgers, Exaone/Qlib services, and registration protocols are untouched.
- The historical experiment is repeatedly observed and exploratory. Real prospective evaluation on post-registration dates would be required to draw investable conclusions.

Commands:
```bash
cd /home/hong/apps/FreeQuantLab-crossmarket-20261021
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m pytest -q
# Original acquisition has already been completed: do not reacquire and overwrite.
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.qlib_cross_market \
 --snapshot-dir reports/cross-market-snapshot-20261021 \
 --report-dir reports/cross-market-TRANSFER-REPLAY
```

Original detailed results: `reports/cross-market-transfer-full-20261021/results.json`.

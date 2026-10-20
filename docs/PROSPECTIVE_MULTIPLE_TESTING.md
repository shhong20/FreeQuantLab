# Historical multi-strategy selection bias & a pre-registered FUTURE research window

Date: 2026-10-20, WSL host `DESKTOP-S51Q473`. **Research only. No live orders or automated strategy promotions.**

## 1. Historic A/B/C and Qlib comparisons remain contaminated by repeated inspection

Earlier BTC/ETH/Samsung Electronics experiments tested multiple strategies and their OOS results have been repeatedly viewed during development. No statistic can reset those historical test windows to truly unobserved data. A low nominal p-value or bootstrap positive fraction from them **is not confirmatory evidence of alpha**.

The existing per-strategy paired moving-block bootstrap remains for descriptive uncertainty, and `diagnostics.familywise_historical_sensitivity` now adds:

- Define the **visible candidate family** (e.g. A, B, C) before computing; the same-exposure buy/hold is benchmark. Risk-matched buy/hold is *another benchmark* rather than an additional strategy.
- Compute each strategy's exact same-date net daily **log-return difference** versus the benchmark in four nonoverlapping chronological test folds.
- Studentize log-excess averages using observed standard deviations. Subtract global candidate means to approximate a zero-excess null.
- Resample 10-day circular blocks **jointly across all candidates**, choosing the same starting block indices for each strategy within each fold. This preserves contemporaneous strategy correlations during resampling.
- Use 1,000 Monte Carlo resamples (fixed RNG seed). For each candidate report individual centered-null tail and the **maximum** statistic across the declared candidate family (familywise max-T sensitivity).
- For zero-variance candidates or nonpositive observed test statistics, **never** award a small tail number.

Caveats: This is an exploratory, approximate block-resampling sensitivity analysis. Stationary null, adequate effective sample size, multiple comparisons across **assets**, omitted historical strategies, prior automated factor searches and repeated OOS inspection are **not** accounted for. No formal frequentist error rate is asserted, and the tails are not probabilities of future profitability. Result flags always set `confirmatory_p_values=false`, `past_oos_already_inspected=true`, `eligible_for_automatic_promotion=false`.

Example:

```bash
.venv/bin/python -m quantlab --config examples/market_research_long.json \
  agent-compare --symbol KRW-BTC --agent-mode mock

# In reports/upbit__KRW-BTC__agent_compare.json:
# diagnostics.familywise_historical_sensitivity.tests
```

A mock agent is a deterministic software wiring test, not AI model performance.

## 2. Versioned, prospective 90-day paper observation plan

Before any new future daily observations, the following **immutable once-registered** prospective plan was generated and checked into GitHub:

- Manifest: `protocols/2026-10-20-btc-eth-90d.json`
- Exactly the existing BTC/ETH **6 independent, frozen** virtual account strategies; each account's identity/frozen settings have a SHA-256 fingerprint.
- Evaluation **starts** with decision UTC day `2026-10-21` and **ends** `2027-01-18` inclusive, 90 daily decision dates.
- Earliest results inspection: `2027-01-19T00:00:00+00:00` (09:00 KST).
- All dates must have recorded forward virtual events for every registered track. Missing days are **missing**, never backfilled with fictitious prices. Any account risk halt makes protocol output `INCOMPLETE_OR_HALTED`.
- For complete tracks, compare the 90 daily observation window with exactly the same-symbol 20%-exposed buy-and-hold virtual account. A descriptive net-return and log-excess table is returned **only after the cutoff**.

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m quantlab --db state/shadow.db \
  protocol-inspect --protocol protocols/2026-10-20-btc-eth-90d.json
```

Before the cutoff this returns `LOCKED_UNTIL_FUTURE_CUTOFF`, **with no interim protocol performance figures**. The original `state/shadow.db` and the existing paper timer/dashboard still work and the protocol does not place or modify orders.

The protocol was created by the one-shot command:

```bash
.venv/bin/python -m quantlab --db state/shadow.db \
  protocol-lock --protocol protocols/2026-10-20-btc-eth-90d.json
```

**Do not rerun to replace a plan.** Any existing target path causes a failure, and its hash is checked on each read. The manifest is versioned in GitHub, providing an externally dated public audit trail; SHA-256 alone **does not** prevent a malicious actor from rewriting both document and hash outside the repository. A new version/experiment requires a separately named manifest and a new prospective start, never a backdated modification.

## 3. Critical distinction: prospective != blinded / statistically independent

The dashboard still displays daily virtual returns in real time. Therefore someone can see outcomes before Jan 19 and change their research hypothesis or trading strategy externally. **This 90-day plan is predeclared, but NOT a blinded untouched holdout.** The code explicitly labels `blinded_holdout=false` and `confirmatory_inference_valid=false`. Do not treat a positive result as investable alpha proof or as an experiment with guaranteed type-I-error control. A future truly protected blinded dataset or third-party timestamped, non-viewable test process would need additional governance.

The paper universe is already selected using prior research. There is survivorship/selection bias, realistic execution/spread/market impact are not guaranteed by public tick-price paper simulation, and the protocol does not simulate live exchange fills.

## 4. Safety and reproducibility

All operations are local on WSL with no paid APIs. `protocol-lock` uses a single-use exclusive filesystem create; `protocol-inspect` only accesses the SQLite database in read-only mode. Neither method supports credential management, broker API calls, strategy activation, automatic account resets or trading.

Offline CI tests cover max-T joint resampling, consistency, candidate alignment, duplicate registration refusal, hash changes, altered track configs, future cutoff, incomplete observations and synthetic complete future ledgers. Never fabricate real-market alpha from those synthetic test fixtures.

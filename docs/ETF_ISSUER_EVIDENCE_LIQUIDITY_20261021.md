# FreeQuantLab · Official ETF issuer evidence and crypto liquidity limits (2026-10-21)

**Research-only. No executable orderbook, no retrospective adjusted profit claim, no model promotion or actual orders.**

## 1. ETF issuer identity obtained — but total-return still BLOCKED

We fetched actual HTML from the fund manager's official HTTPS host and saved the SHA-256 of each product page at the time of retrieval. All three legal ticker-to-product names matched:

| ETF | Official issuer product | Identity | Complete historical payouts/splits |
|---|---|---|---|
| 069500 | [Kodex 200](https://m.samsungfund.com/etf/product/view.do?id=2ETF01) | PASS | **NOT VERIFIED** |
| 114800 | [Kodex 인버스](https://m.samsungfund.com/etf/product/view.do?id=2ETF20) | PASS | **NOT VERIFIED** |
| 132030 | [Kodex 골드선물(H)](https://m.samsungfund.com/etf/product/view.do?id=2ETF24) | PASS | **NOT VERIFIED** |

Issuer confirms that **market price returns do not include cash distributions, while the reference NAV return is presented with distributions reinvested**. `069500` includes quarterly distribution policy, `114800` is a *daily inverse futures* exposure, and `132030` is *gold futures currency hedged* with roll risk. An issuer webpage and a scheduled distribution policy **do not provide a complete historical list of entitlement, ex, and payment dates and cash-per-share amounts**, nor does Naver OHLCV alone establish whether its historical prices are split-adjusted or actual executable unadjusted prints.

Audit: `quantlab/etf_issuer_evidence.py` obtains immutable fingerprint entries and passes them into `quantlab/etf_price_audit.py`, which still returns **`BLOCKED_TOTAL_RETURN_VERIFICATION`**. No missing cash distributions are imputed as zero. Raw OHLCV from Naver for 069500/114800/132030 share 2,999 dates (2014-07-28 to 2026-10-20) and retain SHA-256 checks. The external XKRX calendar package disagrees on 2026-06-03 and 2026-07-17, which are recorded as holiday-library differences rather than fabricated missing bars.

Report files on WSL (not committed, official webpages may update):
- `reports/etf-issuer-evidence-20261021/issuer_pages.json`
- `reports/etf-issuer-evidence-20261021/audit.json`

Issuer verification establishes product identity and policy, not that the entire 2014–2026 economic data stream is ready. **No ETF backtest re-run or publication as investable total return is enabled.**

## 2. Ex-post execution *capacity* stress on existing crypto order traces

This does **NOT** rerun the portfolio with realistic execution: no verified market depth is available and the original continuous model filled all hypothetical orders next-open. Instead we audit every original hypothetical order in four Upbit KRW spot assets (BTC/ETH/XRP/ADA), using the immutable 3,000 UTC-day OHLCV. The result must be clearly separated from any return estimate.

- The proposed participation limit is `0.25%`, `1%`, or `5%` of the **minimum** of (i) the preceding **20 completed days' median daily KRW notional** using close×base volume and (ii) the **actual fill-day whole-day open×base volume**. This second number is known **only after the fill date** and is permitted only in this historical audit, **not a causal real-time trading decision**. Full-day volume says nothing about how much is executable at the opening quote.
- In each (asset, date) group, cumulative order notionals compete for the same day's participation cap: there is no free repeated execution of volume for each order.
- If any order crosses the participation cap, the old trade is labeled **`NOT_EXECUTABLE_AT_CAP`**. We do not carry forward the original reported profit on the assumption that this blocked order filled.
- Additional adverse cost proxy is explicitly specified as `5bps + 40bps × sqrt(min(1, trade_notional / prior20_median_notional))`, **in addition to** existing model fees/slippage. This is not a calibrated order book impact estimate and is *not* deducted from the old equity curve. Only the total KRW extra-cost proxy is printed and `new_net_roi_not_computed=True`.
- Invalid volume, duplicated/inconsistent order notional, missing dates, incorrect fees, or altered original source CSV SHA-256 all trigger fail-closed.
- To test larger notional, we **scale the historic order tickets and costs**, not claim to have replayed an actually larger funded portfolio; use multipliers 1/10/100/1000 of the previous 1 million KRW account. Once an order would fail, the original portfolio path is no longer executable, and a genuine fill-aware account **must be recomputed** before presenting any ROI.

## 3. Observed volume-screen rejects — 15bps existing fee, 1% daily participation cap

| Nominal starting portfolio (proportional ticket stress only) | Risk-capped held positions | MA20/80 | Qlib |
|---|---:|---:|---:|
| KRW 1m | 0 | 0 | 0 |
| KRW 10m | 0 | 0 | 0 |
| KRW 100m | 0 | 1 | 2 |
| KRW 1bn | 1 | 25 | 173 |

At 0.25% daily participation, 1bn proportional sizing breaches for **8 / 79 / 499** of the held / MA / Qlib example's respective orders. At 5%, 1bn still breaches **0 / 3 / 11**. These are counts, not loss percentage forecasts. Qlib's high turnover and scaled-up ticket stress produce much more capacity pressure. No genuine recorded orderbook/slippage, actual trading, or automatic promotion has been performed.

At 1m size and 15bps baseline, no order breaches the even stricter 0.25% turnover cap, but the adverse cost proxy adds roughly KRW **1,587** / **16,700** / **95,331** for held / MA / Qlib respectively under the hypothetical **all-filled** condition. It would be incorrect to subtract these amounts from final NAV and call the result an exact new backtest because reduced cash and later position sizing have not been recalculated.

## 4. Reproduction

```bash
cd /home/hong/apps/FreeQuantLab-liquidity-20261021
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m pytest -q

# No network; immutable stored OHLCV and original continuous account intents
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.liquidity_audit \
 --report /home/hong/apps/FreeQuantLab-continuous-20261021/reports/continuous-crypto-full-20261021/results.json \
 --output-dir reports/NEW_LIQUIDITY_AUDIT_NAME

# HTTPS fetch of current three official Kodex product pages
PYTHONPATH=. /home/hong/apps/FreeQuantLab/.venv/bin/python - <<'PY'
from quantlab.etf_issuer_evidence import save_evidence
print(save_evidence(
    '/home/hong/apps/FreeQuantLab-crossmarket-20261021/reports/cross-market-snapshot-20261021',
    'reports/NEW_ETF_ISSUER_EVIDENCE'))
PY
```

### Next acceptance gates

1. Confirm official complete per-event ETF cash distributions, record/ex/payment dates, corporate actions, adjusted prices, taxation and exchange-calendar version; then build a total-return cash-flow ledger.
2. For crypto, obtain executable quote/LOB/trade feeds and construct a fill-aware, partial-fill/pending-order account whose cash/units, costs and risk breach path are recomputed after every order rather than only **auditing** past successful orders.
3. Demand an independently pre-registered future period (not already tuned) before strategy promotion. Existing basic/Exaone/Qlib timers, shadow ledger and research-only virtual accounts remain unchanged.

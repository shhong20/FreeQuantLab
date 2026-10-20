# Qlib decision replay and three-lane same-day forward comparison

**Implemented 2026-10-20 on WSL.** All balances/orders are *virtual*. Not investment advice, real fills, RD-Agent, or proof of profitable strategies.

## 1. New Qlib prediction-to-event consistency

For **each new, truly completed UTC decision day**, the *manual-only* Qlib runner now verifies and saves the nine causal feature values used by the real frozen Qlib LightGBM checkpoint. Saved evidence is split across four tables **in one SQLite transaction**:

| Table | Evidence saved |
| --- | --- |
| `shadow_events` / `shadow_accounts` | Virtual action, model-derived 0/1 target, simulated price, fees, holdings and equity |
| `qlib_model_decisions` | Frozen model/manifest SHA, probability, fixed threshold 0.55, target, UTC timestamp |
| `qlib_inference_inputs` | Exact 9-feature vector and SHA-256 (historical training feature-code hash is in original model manifest) |
| `shadow_quote_evidence` | Observed public last-traded Upbit ticker, previous completed close, observed timestamp and asset/day |

All four writes **commit together or roll back together**. If a simulated-fill transaction fails halfway through, previously entered evidence is not left behind, so retrying does not silently fabricate fills. The original registered 90-day experiment and Exaone factor lab are **not modified** by this process.

`quantlab.qlib_decision_audit.audit_decisions` is strictly **read-only**. On restart it independently reloads the *original* sealed Qlib booster/golden vectors and replays every saved 9-feature input, checking that:

- the model and manifest hashes match the separately frozen Qlib plan
- the re-evaluated probability matches the saved probability
- saved probability >= 0.55 gives the same target stored in the Qlib model decision **and** the virtual trade event
- virtual price and UTC timestamps match saved public quote evidence
- no event, input, model decision or quote is missing, orphaned or duplicated

Any mismatch **fails closed**: manual Qlib observation runs reject model/data evidence tampering before new public market data is fetched. Missing feature-evidence records for previously recorded Qlib events are treated as gaps, not reconstructed from later hindsight.

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m quantlab.qlib_lab status \
  --plan protocols/2026-10-20-qlib-lab.json \
  --db state/qlib_lab_shadow.db
```

Because no genuinely new forward session has been recorded yet, `decision_replay.status` is `AWAITING_FIRST_DECISION`, not a successful real-market signal assessment. A test fixture using the **actual committed BTC+ETH boosters** and hypothetical future OHLCV passed full signal/event/replay consistency checks in a temporary database. That fixture is **not** counted as actual forward observations.

## 2. Read-only three-lane coverage and held-out comparison

`quantlab.lane_comparison.compare_three_lanes` checks three **separate SQLite ledgers** and their fixed manifests:

- `state/shadow.db`: **6** registered original BTC/ETH virtual accounts (benchmark + MA trend + factor per symbol)
- `state/agent_lab_shadow.db`: **2** separately registered local Exaone-declared factor accounts
- `state/qlib_lab_shadow.db`: **2** original-checkpoint Qlib LightGBM virtual accounts

For each asset, there are **5 independent virtual strategies**. The comparison checks all 10 accounts' immutable identities, the original 90-day registration window, risk/initial fictional cash/position fraction, assumed fees+slippage, and recorded UTC decisions. The Qlib-only lane must also pass full saved model-decision replay. The old protocol itself is never edited or re-registered.

To inspect observation coverage now:

```bash
.venv/bin/python -m quantlab.lane_comparison
```

Read-only local dashboard: `GET http://127.0.0.1:8788/api/research-lanes`.

### Release of descriptive outcomes is strictly gated

**Before** `2027-01-19T00:00:00Z`, this feature returns `WAITING_FOR_FUTURE_CUTOFF` with **only counts and due-date coverage**, with `net_return_by_strategy=null`. It does **not** expose 90-day model-vs-baseline scores or align incomplete datasets to generate attractive returns.

**After** the registered cutoff, descriptive comparison may be produced only when all 10 accounts:
- have recorded all **exactly 90 UTC decision dates** from 2026-10-21 to 2027-01-18 with no risk halt, no synthetic backfill and no gaps
- pass separate full ledger/account audit and Qlib replay check, preserving the registered account identities
- have identical initial virtual cash, maximum 20% exposure, risk stop and identical assumed fee/slippage rates
- each recorded virtual trade has an **actual saved public ticker evidence row for the same symbol/UTC date and session**, with event and quote values in agreement. Missing or contradictory evidence blocks returns; it may not be recreated after the fact.
- use reasonably equivalent public ticker observation prices for the same BTC or ETH UTC day: maximum cross-lane divergence <=20 basis points. If outside tolerance, reports are withheld as **not comparable**.

Only then can a descriptive, **nonconfirmatory** 90-day return and same-symbol 20%-exposed buy/hold return-difference table be shown. Differences in quote timestamps remain a limitation even within the 20-bps threshold.

These are research maturity checks, **not statistical proof or automated trading permission**. The model/agent strategies were selected based on earlier research, the dashboard displays other interim virtual-account results, and public last trade is not executable order-book data. No broker orders or strategy promotion are possible.

## 3. Failure, recovery and CI

- Restarting a process does not re-pick models, re-register accounts or generate earlier missing daily events.
- A missing/altered Qlib checkpoint blocks inference rather than silently retraining.
- SQL transaction rollback test deliberately aborts the feature-evidence insert and verifies there is **no orphaned model prediction, quote or simulated trade, and account capital is intact**.
- Tampering a saved probability or virtual target is detected even if the usual cash/units ledger arithmetic remains consistent.
- A strict three-lane comparison *refuses* to mix different trading days, incomplete 90-day sequences, unequal modeled costs, altered virtual account identities, or materially mismatched quote prices.
- CI includes exact actual Qlib frozen BTC+ETH checkpoint re-inference from saved golden vectors and an offline next-day run with real model inference on **synthetic** OHLCV, plus separate full-coverage simulated 90-day tests. These are verification tests, not real forward performance.

**First genuinely eligible original protocol day**: 2026-10-21 UTC; its first observation opportunity is after the UTC candle close at **2026-10-22 09:00 KST**, with missed-day deadline at 10:20 KST. The other two experimental lanes have **no automatic timers** and thus will not become fully comparable unless separately run on eligible days; do not backfill missing dates retrospectively.

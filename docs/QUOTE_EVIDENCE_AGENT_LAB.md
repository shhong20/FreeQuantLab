# Virtual quote evidence and isolated local-Agent candidate research lane

Date: 2026-10-20. WSL host DESKTOP-S51Q473. All accounts here are **fictional** and no real exchange orders are available.

## 1. New price-evidence records for NEXT observed paper cycles

Previous versions recorded only `shadow_events.observed_price`, virtual BUY/SELL/HOLD and aggregate assumed fee+slippage. They did **not** store an immutable paired previous completed UTC candle close or exact ticker observation timestamp. Historical execution slippage must not be retroactively fabricated.

From this change onwards, the **first new observed public Upbit quote** used by a paper cycle is additionally recorded in SQLite table `shadow_quote_evidence`, keyed by (asset symbol, decision UTC day). It saves:

- previous **completed** UTC daily close, validated from Upbit OHLCV
- observed Upbit **public last-traded ticker** and its UTC session date
- observation timestamp, immutable once captured for that symbol/decision day
- the explicit source label `upbit_public_ticker`

It is saved before the corresponding virtual account events. On a retry with no outstanding tracks, no additional quote request or duplicate event is made. If a partially failed day left a previously frozen quote evidence row, a new quote on the same day will **fail closed**, rather than quietly revise the evidence or append inconsistent virtual fills. This condition requires manual review and may leave a genuine observation gap.

`quantlab.quote_quality.execution_proxy_quality()` reads the source ledger with `mode=ro`, compares every recorded event to the referenced quote, verifies modeled costs against `fee_bps+slippage_bps`, and reports:

- number of recorded virtual events with/without quote evidence
- number of unique public quotes
- maximum absolute **public ticker vs previous daily close gap** in basis points
- latest quote acquisition delay since UTC daily rollover, in minutes
- virtual BUY/SELL notional and **assumed** cumulative modeling costs
- impossible timestamps, mismatched quotes, a stranded quote without event

**These are NOT actual execution-quality metrics.** A last-traded ticker does not reveal executable bid/ask, order-book depth, partial fills, order acceptance, exchange latency or market impact. In particular, a ticker-to-prior-close gap is a market movement/overnight proxy, **not actual slippage**. `simulated_cost` reflects hardcoded assumed fee/slippage, not measured exchange execution cost. The API always signals `not_actual_fill_quality=true`.

Legacy events without evidence are reported with `legacy_events_without_quote_evidence` and excluded from gap/timing calculations; no fake evidence is created. A mismatched stored quote, impossible timestamp or cost discrepancy makes the independent Watchdog `DEGRADED`, emits a critical alert and withholds newly generated daily performance reports until reviewed.

Read-only endpoint: `GET http://127.0.0.1:8788/api/quote-quality`; dashboard also summarizes the quote evidence.

## 2. Real local Exaone proposal, completely separate from registered 90-day experiment

The installed local Ollama instance has an Exaone 3.5 model. Using **only historical training summaries from 240 complete candles**, the allowlisted local-Agent JSON proposal was generated and sealed in:

`protocols/2026-10-20-exaone-agent-lab.json`

The FIRST valid factor proposal per asset is recorded; no search over holdout/test outcomes, no generated code execution. The current proposals happen to be identical:

- BTC: `r5 > 0.02` (5-day price return greater than 2%)
- ETH: `r5 > 0.02`

Each factor maps to an allowlisted `kind=factor` signal and has an SHA256-fingerprinted manifest and track_id. This proposal is **not claimed to be profitable**, is not a substitute for actual Qlib/RD-Agent results, and has not been promoted to the original experiment.

The two new virtual accounts are kept exclusively in `state/agent_lab_shadow.db`, **NOT** the production `state/shadow.db`. Account verification checks the frozen factor, asset set and cash/risk settings before fetching new prices. Database path guards refuse production and any other target basename.

To inspect the existing isolated Agent research lane:

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m quantlab.candidate_lab status \
  --plan protocols/2026-10-20-exaone-agent-lab.json \
  --lab-db state/agent_lab_shadow.db --production-db state/shadow.db
```

For a **manual observation cycle only**, which cannot fill earlier calendar days and which applies the same data/quote validation as existing Shadow Paper:

```bash
.venv/bin/python -m quantlab.candidate_lab cycle \
  --plan protocols/2026-10-20-exaone-agent-lab.json \
  --lab-db state/agent_lab_shadow.db --production-db state/shadow.db
```

The earlier one-time setup was:

```bash
.venv/bin/python -m quantlab.candidate_lab propose \
  --plan protocols/2026-10-20-exaone-agent-lab.json \
  --model exaone3.5:latest --llm-url http://127.0.0.1:11434/v1
.venv/bin/python -m quantlab.candidate_lab register \
  --plan protocols/2026-10-20-exaone-agent-lab.json \
  --lab-db state/agent_lab_shadow.db --production-db state/shadow.db
```

**Do NOT run propose/register again**. The manifest file refuses overwrites, and registration is permitted only on its original same UTC calendar day, never backdated. The 90-day BTC·ETH paper accounts retain their exact original identities and schedules. The lab has **NO automatic systemd timer**, **NO auto-strategy promotion**, and **NO broker-order credentials**.

## 3. Qlib isolation boundary

Actual Qlib LightGBM research remains available in `qlib_walk_forward` and the existing four-fold causal test pipeline. **It is not connected as a forward virtual account or marketed as a deployed candidate.** Before enabling a Qlib daily account, a separate implementation must freeze and hash the trained model checkpoint, preprocessing/features, training cutoff, and inference outputs; verify the same prediction after restart; validate inference with a completed UTC bar only; and create a separately registered paper-only account. The Exaone factor account is NOT being mislabeled as Qlib.

## 4. Safety checklist

```bash
.venv/bin/python -m pytest -q
.venv/bin/python - <<'PY'
from quantlab.quote_quality import execution_proxy_quality
from quantlab.ledger_audit import audit_shadow_ledger
for p in ['state/shadow.db','state/agent_lab_shadow.db']:
    print(p,audit_shadow_ledger(p)['status'],
          execution_proxy_quality(p)['status'])
PY
```

Neither lane has enough genuine post-registration future observations to support financial conclusions yet. Nothing in these components can execute real orders.

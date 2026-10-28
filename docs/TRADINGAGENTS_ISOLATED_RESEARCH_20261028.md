# TradingAgents × FreeQuantLab isolated research lane (2026-10-28)

## Scope

This is a **standalone offline exported-decision evaluation adapter**, not an
installation of upstream TradingAgents, not a live LLM run, and not an update
to FreeQuantLab's frozen 90-day BTC/ETH cohort, Qlib, KRX minute collection,
paper accounts, production runtime or any timer.

Implementation: \`quantlab/tradingagents_lab.py\`; tests:
\`tests/test_tradingagents_lab.py\`.

**Decision provenance is retrospective and unverified.** TradingAgents'
historical news/social context may not reproduce what was known at a
historical decision time. The output flag \`lookahead_verified=false\` and
\`paper_eligible=false\` cannot be overridden. The resulting metrics are
engineering comparisons, not causal OOS performance evidence.

## Input contracts

OHLCV: a UTF-8 CSV containing the unique **completed** daily session dates
\`date,open,high,low,close,volume\`. Date is YYYY-MM-DD, 120+ unique rows.
Input is validated and its exact SHA-256 is recorded; the caller must
independently certify corporate actions, source rights and availability.
The local adapter never downloads or changes historical prices.

Single-agent and TradingAgents multi-agent outputs are separate JSONL files,
one JSON object per decision:

\`\`\`json
{"ticker":"005930","decision_date":"2025-05-06","rating":"Buy","generated_at":"2026-10-27T09:00:00+09:00","model_id":"your-pinned-local-model","config_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","provenance_kind":"retrospective"}
\`\`\`

\`generated_at\` is the actual generation timestamp, not the claimed date of
knowledge. Unverified forward labels, unsupported ratings (including REVIEW),
unrecognized/missing keys, mismatched ticker, duplicates and out-of-range
decision dates are rejected.

Price series is the sole data feed. No LLM API, Yahoo, KIS, Upbit, broker
credentials, paper database, memory log or automated order endpoint is touched.

## Run

\`\`\`bash
cd /home/hong/apps/FreeQuantLab-tradingagents-20261028
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
$PY -m quantlab.tradingagents_lab \
  --csv /absolute/path/to/frozen-completed-daily.csv \
  --ticker 005930 \
  --single /absolute/path/to/single-agent.jsonl \
  --multi /absolute/path/to/tradingagents.jsonl \
  --out /home/hong/apps/FreeQuantLab-tradingagents-20261028/reports/tradingagents-lab/run001.json

$PY -m pytest -q tests/test_tradingagents_lab.py
\`\`\`

Output may be created only as a new JSON file directly inside
\`reports/tradingagents-lab/\`; no overwrites. This directory is excluded
from version control.

## Evaluation assumptions / restrictions

- The last 20% of the data is a fixed **retrospective** decision window.
  This split does **not** make decisions originally generated with today's
  knowledge causal historical observations.
- A: fixed MA10/40 baseline. B: single model. C: TradingAgents full or
  partial agent output. D: MA10/40 allowed only while C permits a long.
- Buy/Overweight -> long up to FreeQuantLab default 20% exposure;
  Sell/Underweight -> cash; Hold -> previous position; expired/missing
  decisions -> cash. No short positions or leverage.
- The unmodified \`quantlab.engine.simulate\` measures next-open
  executable-price *assumptions* with fee/slippage and 2×-cost stress.
  It is NOT true fill evidence. Fairness requires matched decision dates,
  input context and model budget; the adapter does not establish that.
- Old TradingAgents memory logs must **not** be converted to prospectively
  verified signals just because the decision field or a timestamp exists.
- A real prospective run requires a separate write-once, time-stamped
  input snapshot and forecast receipt before the relevant close/entry,
  stable model/config versions, verified reference feeds and independent
  live-order-free shadow evaluation.

Upstream: https://github.com/TauricResearch/TradingAgents (Apache-2.0).
No upstream code was copied or imported by this adapter. Upstream supports
Ollama/OpenAI-compatible endpoints, but local model compatibility, latency
and live execution have **not** been measured in this phase.

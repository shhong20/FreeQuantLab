# Qlib + Korean equity research (WSL, 2026-10-20)

This project now invokes **Microsoft Qlib's real `qlib.contrib.model.gbdt.LGBModel`**, not merely its Python import. The bridge creates a `DatasetH`-compatible *in-memory* dataset from FreeQuantLab causal OHLCV features, then uses Qlib's LightGBM training, early stopping and `predict` methods.

It **does not** yet reproduce the official stock-ranking Alpha158/Alpha360 benchmark or Microsoft RD-Agent. Those require cross-sectional investable universes, corporate-action-adjusted data, standardized benchmark definitions and additional leakage/selection tests.

### Reproduce on live public historical feeds

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m pip install -e '.[dev,ml,qlib]'
.venv/bin/python -m pytest -q
.venv/bin/python -m quantlab --config examples/market_research_long.json qlib-compare --symbol KRW-BTC
.venv/bin/python -m quantlab --config examples/market_research_long.json qlib-compare --symbol KRW-ETH
.venv/bin/python -m quantlab --config examples/market_research_long.json qlib-compare --symbol 005930
```

`qlib-compare` trains Qlib LGBModel on historical information with an inner train/validation split, holds back two future opening prices from the training boundary, predicts on four disjoint outer walk-forward test windows, and compares against a fixed-exposure buy-and-hold and a validation-selected baseline. Fees, slippage, and double-cost sensitivity are included. This is an exploratory study and not independently established profitability.

Reports: `reports/<provider>__<symbol>__qlib_compare.json`; CSV snapshots for reproducibility in `reports/snapshots/`. SHA-256 of the consumed dataset is in the report. These outputs and the SQLite-backed MLflow experiment state are excluded from Git.

### Sources and boundaries

- **Upbit**: public OHLCV; no account keys needed.
- **Naver equity chart**: unofficial public `fchart.stock.naver.com` chart history; now accessed only for RESEARCH, not execution. It is not an authorized commercial redistribution data license, has no published stability guarantee, and historical corporate-action adjustment may not be consistent. Excludes today's KRX session, parses EUC-KR XML conservatively, validates OHLCV. Check terms before deployment beyond personal research.
- **Stooq/Yahoo US stocks**: intermittent bot challenges/429s on the WSL IP; not a dependable production feed. KIS remains the recommended authenticated domestic trading data path, using user-provided keys that are never committed.
- **Trade safety**: Qlib and Naver research outcomes cannot enable real orders or paper promotion. The only existing forward-paper path remains separately gated and limited to Upbit/KIS. **No live order endpoints exist.**

### Research validity caveats

Only the single-asset directional prediction problem is modeled, not Qlib cross-sectional stock selection. A 4-fold OOS win with few trades is a weak observation, not a statistically reliable edge. Corporate actions, delisting/universe biases, financing, real quote timing and partial fills are not fully modeled. Seek long forward-paper results before any live-trading implementation.

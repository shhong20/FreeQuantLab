# WSL deployment and real-market status (2026-10-20)

## Host
- Authorized host: `DESKTOP-S51Q473` WSL2
- Project root: `/home/hong/apps/FreeQuantLab`
- Python environment: `.venv` (Python 3.12 from local conda)
- GitHub: `shhong20/FreeQuantLab` `main`
- GPU: GTX 1080 8GB, **not** a separate RTX A6000 server
- Ollama: loopback `http://127.0.0.1:11434/v1`, locally available `exaone3.5:latest` and `qwen3.5:4b`
- Dependency installs: `pip install -e '.[dev,ml,qlib]'` for `lightgbm` and `pyqlib==0.9.7`

## Offline checks
```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m pytest -q
.venv/bin/python -c "import qlib; print('Qlib import OK')"
```

`pyqlib` is installed, but the original Microsoft Qlib dataset handler/portfolio simulator and Microsoft RD-Agent **are not integrated into FreeQuantLab** at this point. Import verification is not model or portfolio backtest validation.

## Actual market data research
```bash
.venv/bin/python -m quantlab --config config.public.json research
.venv/bin/python -m quantlab --config config.public.json agent-compare --symbol KRW-BTC --agent-mode mock --with-lightgbm
```

As of 2026-10-20, Upbit BTC/ETH candles were reachable but the baseline selected strategies did not meet paper-eligibility gates. In 4 OOS BTC folds:
- A baseline -1.264%; B mock -4.202%; C mock -3.916%; equal-exposure buy-and-hold -6.889%.
- The B/C **mock** results cannot be treated as evidence that any real LLM trader works.

Stooq returned a browser JavaScript proof-of-work page on this host. Yahoo Finance returned rate-limit errors during the attempt. Therefore US stocks have no verified current data feed. KIS requires end-user credentials and has not been verified.

## Local-model caveats
`qwen3.5:4b` timed out on one brief OpenAI-compatible request (58-second request limit); `exaone3.5:latest` returned structured factor JSON inside a Markdown code fence in about 5 seconds on the local Ollama native endpoint. The local Agent decoder accepts only standalone strict JSON or a single complete fenced JSON block and still verifies the allowlisted factor fields. It does not execute model output as Python code.

To run live **agent research only** (no real orders):
```bash
.venv/bin/python -m quantlab --config config.public.json agent-compare --symbol KRW-BTC \
  --agent-mode local --llm-url http://127.0.0.1:11434/v1 \
  --llm-model exaone3.5:latest --with-lightgbm
```

Each of 4 folds requires 2 model calls. Runs may be slow; failed/invalid responses stop the experiment rather than silently substituting mock signals. Full local A/B/C profitability is not yet validated.

## Trading safety
Do not add broker order credentials to tracked files. No live broker execution exists in the project. Freeze/paper gates are development-only and not sufficient for live capital.

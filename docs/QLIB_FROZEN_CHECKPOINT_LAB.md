# Qlib Frozen Model Checkpoints · Restart Proof · Isolated Paper Lane

**2026-10-20 · DESKTOP-S51Q473 · offline/local research only; no broker orders**

## What was actually trained

Unlike a mock/heuristic factor or Exaone-generated factor, this lane trained the **real Microsoft Qlib 0.9.7 `qlib.contrib.model.gbdt.LGBModel`**, backed by LightGBM 4.7.0, using public Upbit BTC/ETH **240 consecutive completed UTC daily candles through 2026-10-19**. Feature engineering is FreeQuantLab's *causal* nine-feature input, **not Qlib Alpha158/Alpha360**. Label is the direction from the *next session open to the following session open*. Last two bars (whose future outcomes were not known at freeze time) are not eligible for training.

- Inner split: 161 train labels and 46 validation labels per asset; no later bars
- Default threshold: 0.55. No OOS search to select it
- BTC LightGBM: 29 trees after early stopping
- ETH LightGBM: 1 tree after early stopping (may be a weak or near-constant model; do not call it strong)
- No test/holdout return analysis, no evidence of trading profitability

Training was one-time. `freeze_qlib_bundle` creates a new directory atomically and refuses overwrites. Live/new-bar inference only loads its existing model; it can **never retrain**.

## Immutable Qlib artifact set

The training bundle for each asset has three data files: `model.txt` (LightGBM Booster model serialized via `model_to_string`), `golden.json` (24 historical input feature rows and expected predictions) and `manifest.json` (SHA256 of both files, schema/source hash, asset/cutoff, runtime versions, fixed threshold and flags). The manifest itself is SHA-256 sealed and linked into the separate frozen strategy plan:

`protocols/2026-10-20-qlib-lab.json`

**Source copies are versioned** in `models/qlib/KRW-BTC` and `models/qlib/KRW-ETH` for recovery and independent SHA verification. The running models remain under `state/qlib_models` on the WSL server (excluded from Git). The registration manifest currently references these exact local directories. Model artifacts contain only model weights and historical public feature/prediction samples, **no exchange credentials**.

```bash
cd /home/hong/apps/FreeQuantLab

# Inspect each immutable checkpoint in a freshly started Python process.
.venv/bin/python - <<'PY'
from quantlab.qlib_frozen import read_qlib_bundle
for s in ("KRW-BTC","KRW-ETH"):
    spec, model = read_qlib_bundle("state/qlib_models/"+s)
    print(s, spec["sha256"], model.num_trees())
PY

# These committed snapshots preserve the exact underlying file bytes.
sha256sum state/qlib_models/KRW-BTC/model.txt models/qlib/KRW-BTC/model.txt
sha256sum state/qlib_models/KRW-ETH/model.txt models/qlib/KRW-ETH/model.txt
```

Any model-file tampering, altered golden predictions, changed feature order or feature source, changed hash, different ML library versions or changed runtime permissions **fails closed**. A version mismatch requires a new explicitly reviewed experiment; never auto-upgrade silently.

## Separate, manual Qlib virtual accounts

The **two registered Qlib lab accounts** live only at `state/qlib_lab_shadow.db`, with fixed `qlib_lgbmodel_055` strategy IDs and distinct BTC/ETH SHA256 model fingerprints.

```bash
# No network needed to inspect registration and ledger.
.venv/bin/python -m quantlab.qlib_lab status \
  --plan protocols/2026-10-20-qlib-lab.json --db state/qlib_lab_shadow.db

# Manual run (public Upbit read only, virtual account updates only if
# a genuinely NEW completed UTC daily bar is available).
.venv/bin/python -m quantlab.qlib_lab cycle \
  --plan protocols/2026-10-20-qlib-lab.json --db state/qlib_lab_shadow.db
```

The first real-day cycle on 2026-10-20 yielded `WAIT_NEW_BAR_OR_ALREADY_RECORDED` for both, correctly recording **zero** fake events. At the first genuine new bar, the cycle will:

1. Verify plan hash, individual bundle and golden SHA256, original trained model and exact frozen virtual account identity **before fetching live price**.
2. Validate the completed UTC day + full OHLCV, produce a single prediction from the frozen model, and apply the fixed threshold to compute long(20%)/cash.
3. Fetch public read-only Upbit last-traded ticker with session check, validate reasonable price, record per-day prediction/model hashes in `qlib_model_decisions`, and record an immutable public quote observation.
4. Apply virtual-only cash/units/cost model under SQLite transactions, with one event per asset/day and no historical backfill or overwrite. Prevent generic Shadow Paper from running Qlib frozen accounts.

This is **manual only**. There is deliberately no `qlib_lab.timer` or runner connected to the existing three automatic services.

## Lifecycle and isolation

| Ledger | Registered accounts | Role |
| --- | ---: | --- |
| `state/shadow.db` | 6 | Existing original 90-day BTC/ETH protocol. Must remain untouched |
| `state/agent_lab_shadow.db` | 2 | Exaone-derived rule factor research; manual only |
| `state/qlib_lab_shadow.db` | 2 | Actual frozen Qlib LightGBM checkpoint; manual only |

All of these are **hypothetical investment** accounting, not investment recommendations or brokerage APIs. Initial capital in each account is a **separate fictional balance** and may not be summed into one implemented account. BTC/ETH trading data and the 90-day preregistered evaluation have different registration/validation histories; they do not form a single blind, comparable performance trial.

## Recovery

If the local checkpoint disappears while the virtual account still exists, **do not retrain**. Restore model bytes from the committed `models/qlib/<symbol>/` directories to their exact expected local `state/qlib_models/<symbol>/` directories using a manually reviewed copy operation *only when the target is absent*, then run `read_qlib_bundle` and `qlib_lab status` to check the hashes before any paper run. Existing registered account metadata and dates must never be rewritten or moved backward.

## Caveats

- Qlib LGBModel's in-memory dataset wrapper adapts FreeQuantLab causal features, not Qlib cross-sectional factor pipelines.
- Public ticker is **not executable bid/ask**. All fees/slippage are fixed assumptions; model execution in virtual ledger is not a real-world fill.
- Golden historical predictions test deterministic loading, not predictive skill.
- Local model files are protected against *accidental* edit/hash drift, not a malicious user with permissions to rewrite all hashes and Git history.
- Model training uses 240 past bars; rigorous model calibration, stability, and genuinely fresh forward performance require additional future observations.

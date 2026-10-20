"""Chronological train / validation / completely untouched test evaluation.

Next-bar OPEN execution, one-period holding, fixed directional exposure (long/cash).
No tuning on test period, no random data shuffling, no short selling, no leverage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .features import FEATURE_COLUMNS, build_features, forward_label
from .settings import Settings


@dataclass(frozen=True)
class Candidate:
    kind: str
    fast: int = 10
    slow: int = 40
    threshold: float = 0.55

    @property
    def key(self) -> str:
        if self.kind == "momentum":
            return f"MA{self.fast}-{self.slow}"
        return f"{self.kind.upper()}-p{self.threshold:.2f}"


CANDIDATES = [Candidate("momentum", 10, 40), Candidate("momentum", 20, 80),
              Candidate("momentum", 30, 100), Candidate("logit", threshold=0.50),
              Candidate("logit", threshold=0.55), Candidate("logit", threshold=0.60),
              Candidate("hgb", threshold=0.50), Candidate("hgb", threshold=0.55),
              Candidate("hgb", threshold=0.60)]


def splits(df: pd.DataFrame, settings: Settings) -> tuple[int, int]:
    n = len(df)
    t = int(n * settings.train_fraction)
    v = int(n * (settings.train_fraction + settings.validation_fraction))
    if n < 250 or t < 120 or v - t < 40 or n - v < 40:
        raise ValueError("At least 250 completed daily bars, including enough validation/test history, required")
    return t, v


def _model(df: pd.DataFrame, train_end_exclusive: int, *, kind: str = "logit") -> Pipeline:
    """Fit only rows with a KNOWN subsequent open-to-open label.

    For a signal on t, next open(t+1) is unknown until future; train through t-2.
    """
    features = build_features(df)
    labels = forward_label(df)
    indices = features.index[(features.index < train_end_exclusive - 2) & features.notna().all(axis=1) & labels.notna()]
    x = features.loc[indices, FEATURE_COLUMNS]
    y = labels.loc[indices].astype(int)
    if len(x) < 70 or y.nunique() != 2:
        raise ValueError("Insufficient ML history or label diversity")
    if kind == "logit":
        classifier = LogisticRegression(max_iter=500, random_state=42, C=0.5)
    elif kind == "hgb":
        classifier = HistGradientBoostingClassifier(max_iter=75, max_leaf_nodes=12,
                                                    l2_regularization=3.0, learning_rate=0.04,
                                                    random_state=42)
    elif kind == "lgbm":
        try:
            from lightgbm import LGBMClassifier
        except ImportError as exc:
            raise ValueError("Optional lightgbm dependency missing: pip install -e '.[ml]'") from exc
        classifier = LGBMClassifier(n_estimators=100, learning_rate=0.03,
                                   max_depth=4, num_leaves=12, min_child_samples=20,
                                   reg_lambda=3.0, verbosity=-1, random_state=42, n_jobs=1)
    else:
        raise ValueError("Unknown ML kind")
    # Tree learners do not require scaling; keep feature names for LightGBM.
    model = Pipeline([("clf", classifier)] if kind == "lgbm" else
                     [("scale", StandardScaler()), ("clf", classifier)])
    model.fit(x, y)
    return model


def signals(df: pd.DataFrame, candidate: Candidate, train_end_exclusive: int) -> np.ndarray:
    if candidate.kind == "momentum":
        c = df["close"].astype(float)
        fast = c.rolling(candidate.fast).mean()
        slow = c.rolling(candidate.slow).mean()
        return ((fast > slow) & fast.notna() & slow.notna()).astype(float).to_numpy()
    if candidate.kind in ("logit", "hgb", "lgbm"):
        model = _model(df, train_end_exclusive, kind=candidate.kind)
        feat = build_features(df)
        valid = feat.notna().all(axis=1)
        sig = np.zeros(len(df), dtype=float)
        if valid.any():
            sig[np.flatnonzero(valid.to_numpy())] = (model.predict_proba(feat.loc[valid, FEATURE_COLUMNS])[:, 1] >= candidate.threshold).astype(float)
        return sig
    raise ValueError(f"Unknown strategy: {candidate.kind}")


def simulate(df: pd.DataFrame, sig: np.ndarray, start: int, end: int, settings: Settings, *, annual_periods: int = 252) -> dict[str, float | int]:
    """At decision close t, next open t+1 executes; PnL through open t+2.

    start..end indices are *decision* indices, not fill indices. end <= len(df)-2.
    Fee/slippage deducted at every exposure change; forced exit fee at final day.
    """
    if not 0 <= start < end <= len(df) - 2:
        raise ValueError("Out-of-bounds backtest window")
    raw = np.asarray(sig[start:end], dtype=float)
    if not np.isin(raw, [0.0, 1.0]).all():
        raise ValueError("No shorting or leverage allowed")
    pos = raw * settings.max_position_fraction
    openp = df["open"].to_numpy(dtype=float)
    future_returns = openp[start+2:end+2] / openp[start+1:end+1] - 1
    trade_size = np.abs(np.diff(np.concatenate(([0.0], pos))))
    orders = int(np.count_nonzero(trade_size > 1e-12))
    fee = (settings.fee_bps + settings.slippage_bps) / 10000
    factors = (1 - trade_size * fee) * (1 + pos * future_returns)
    if pos[-1] > 0:
        factors[-1] *= 1 - pos[-1] * fee  # forced exit cost on allocated fraction
        orders += 1
    eq = np.cumprod(np.concatenate(([1.0], factors)))
    simple = factors - 1.0
    peak = np.maximum.accumulate(eq)
    max_dd = float(np.min(eq / peak - 1.0))
    std = float(np.std(simple, ddof=1)) if len(simple) > 1 else 0
    sharpe = float(np.mean(simple) / std * np.sqrt(annual_periods)) if std > 1e-12 else 0.0
    total = float(eq[-1] - 1.0)
    return {
        "total_return": total, "max_drawdown": max_dd, "sharpe": sharpe,
        "orders": orders, "active_days": int(np.count_nonzero(raw)), "days": len(pos),
        "annualized_return": float(eq[-1] ** (annual_periods / len(pos)) - 1) if eq[-1] > 0 else -1.0,
    }


def _pack(candidate: Candidate, kind: str, result: dict) -> dict:
    return {"strategy": candidate.key, "kind": kind, **result}


def research(df: pd.DataFrame, settings: Settings, *, annual_periods: int = 252) -> dict[str, Any]:
    """Choose on validation ONLY. Report held-out test without using it to choose."""
    train_end, val_end = splits(df, settings)
    results = []
    for c in CANDIDATES:
        try:
            predicted = signals(df, c, train_end)
            valid = simulate(df, predicted, train_end, val_end, settings, annual_periods=annual_periods)
            results.append((c, valid))
        except (ValueError, FloatingPointError):
            continue
    if not results:
        raise ValueError("No candidate could be evaluated")
    # Prevent selection of dormant strategies. Do not peek at TEST for ranking.
    eligible = [(c, r) for c, r in results if r["orders"] >= 2]
    if not eligible:
        return {"status": "REJECT", "reason": "No meaningful validation trades", "validation": [_pack(c, c.kind, r) for c, r in results], "test": None}
    best, val = max(eligible, key=lambda item: (item[1]["sharpe"] - 2 * abs(item[1]["max_drawdown"]), item[1]["total_return"]))
    predicted = signals(df, best, val_end)  # retrain on train+validation, never test labels
    test = simulate(df, predicted, val_end, len(df) - 2, settings, annual_periods=annual_periods)
    # Independent OOS gate. A passing backtest is NOT permission to live-trade.
    test_ok = bool(test["orders"] >= settings.min_test_orders and test["sharpe"] >= settings.min_test_sharpe and abs(test["max_drawdown"]) <= settings.max_drawdown and test["total_return"] > 0)
    validation_ok = bool(val["total_return"] > 0 and val["sharpe"] > 0 and abs(val["max_drawdown"]) <= settings.max_drawdown)
    selected = asdict(best)
    fingerprint = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()[:16]
    return {"status": "PAPER_ELIGIBLE" if test_ok and validation_ok else "REJECT",
            "reason": "Both validation and held-out test passed" if test_ok and validation_ok else "Validation or OOS test gate failed",
            "selected": selected, "strategy_id": fingerprint,
            "split_dates": {"train_end": df.iloc[train_end]["date"].date().isoformat(),
                            "validation_end": df.iloc[val_end]["date"].date().isoformat(),
                            "last_completed": df.iloc[-1]["date"].date().isoformat()},
            "validation_selected": val, "validation": [_pack(c, c.kind, r) for c, r in results],
            "test": test, "train_rows": train_end, "validation_rows": val_end - train_end,
            "test_rows": len(df) - 2 - val_end}


def next_decision(df: pd.DataFrame, result: dict) -> dict:
    if result.get("status") != "PAPER_ELIGIBLE":
        raise ValueError("A rejected research strategy cannot make a paper decision")
    candidate = Candidate(**result["selected"])
    # Train only from labels that can be known at the LAST fully completed close.
    sig = signals(df, candidate, len(df))
    return {"date": df.iloc[-1]["date"].date().isoformat(), "target": int(sig[-1]),
            "strategy_id": result["strategy_id"], "strategy": candidate.key}

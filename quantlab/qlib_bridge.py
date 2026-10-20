"""Actual Microsoft Qlib LGBModel on local read-only OHLCV.

A compact DatasetH-compatible in-memory adapter bridges causal FreeQuantLab features
to Qlib's actual LightGBM training, validation and prediction code. This is NOT
Qlib's Alpha158/Alpha360 cross-sectional dataset or RD-Agent.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .features import FEATURE_COLUMNS, build_features, forward_label
from .settings import Settings
from .quality import audit_dataset_snapshot
from .diagnostics import daily_net_returns, trailing_regimes, regime_attribution, diagnostic_summary
from .engine import Candidate, signals, simulate
from .agent_lab import walk_forward_windows, _baseline_pool, _best_on_validation, _test_signal


@dataclass
class _MemoryQlibDataset:
    train: pd.DataFrame
    valid: pd.DataFrame
    test_features: pd.DataFrame

    @property
    def segments(self):
        return {"train": True, "valid": True, "test": True}

    def prepare(self, segment, col_set="__all", data_key="infer", **kwargs):
        if segment == "train":
            data = self.train
        elif segment == "valid":
            data = self.valid
        elif segment == "test":
            data = pd.concat({"feature": self.test_features}, axis=1)
        else:
            raise ValueError("Unknown dataset segment")
        if col_set in ("feature", "label"):
            return data[col_set]
        if isinstance(col_set, list):
            return data.loc[:, col_set]
        if col_set == "__all":
            return data
        raise ValueError("Invalid column set")


def _initialize_qlib(state_dir: str | Path):
    try:
        import qlib
        from qlib.config import C
    except ImportError as exc:
        raise RuntimeError("pyqlib is required: pip install -e '.[qlib,ml]'") from exc
    path = Path(state_dir).resolve() / "qlib"
    path.mkdir(parents=True, exist_ok=True)
    provider = path / "provider"
    provider.mkdir(exist_ok=True)
    if not C.registered:
        config = {
            "class": "MLflowExpManager",
            "module_path": "qlib.workflow.expm",
            "kwargs": {"uri": "sqlite:///" + str(path / "mlflow.db"),
                       "default_exp_name": "FreeQuantLabResearch"},
        }
        qlib.init(provider_uri=str(provider), exp_manager=config, logging_level=40)
    return path


def qlib_probabilities(df: pd.DataFrame, fit_end: int, state_dir: str | Path) -> np.ndarray:
    """Train only on decisions where the next TWO opens were observed before fit_end.

    A second *inner* validation is cut from past-only data for early stopping.
    Training labels never use opens at or beyond fit_end.
    """
    if fit_end < 250 or fit_end > len(df):
        raise ValueError("Invalid Qlib training boundary")
    from qlib.contrib.model.gbdt import LGBModel
    _initialize_qlib(state_dir)
    df = df.reset_index(drop=True)
    feats = build_features(df)
    labels = forward_label(df)
    inner_split = max(100, int(fit_end * 0.80))
    index = np.arange(len(df))
    known = feats.notna().all(axis=1) & labels.notna()
    train_mask = known & (index < inner_split - 2)
    valid_mask = known & (index >= inner_split) & (index < fit_end - 2)
    if train_mask.sum() < 80 or valid_mask.sum() < 25:
        raise ValueError("Insufficient historical known-label Qlib rows")
    def section(mask):
        x = feats.loc[mask, FEATURE_COLUMNS].astype(float)
        y = labels.loc[mask].astype(int).to_frame(name="LABEL0")
        return pd.concat({"feature": x, "label": y}, axis=1)
    x_test = feats.loc[feats.notna().all(axis=1), FEATURE_COLUMNS].astype(float)
    dataset = _MemoryQlibDataset(section(train_mask), section(valid_mask), x_test)
    model = LGBModel(
        loss="binary", metric="binary_logloss", num_boost_round=70,
        early_stopping_rounds=12, learning_rate=0.04, num_leaves=9,
        max_depth=4, min_data_in_leaf=22, lambda_l2=3.0,
        feature_fraction=1.0, bagging_fraction=1.0, seed=42, num_threads=1,
        verbosity=-1,
    )
    model.fit(dataset, verbose_eval=0)
    predictions = model.predict(dataset, segment="test")
    out = np.full(len(df), np.nan)
    out[predictions.index.to_numpy(dtype=int)] = predictions.to_numpy(dtype=float)
    return out


def qlib_signals(df: pd.DataFrame, fit_end: int, state_dir: str | Path,
                 threshold: float = 0.55) -> np.ndarray:
    if not 0 < threshold < 1:
        raise ValueError("Prediction threshold must be in (0, 1)")
    probs = qlib_probabilities(df, fit_end, state_dir)
    return (np.isfinite(probs) & (probs >= threshold)).astype(float)


def _score(df, sig, start, end, cfg, annual_periods):
    return simulate(df, sig, start, end, cfg, annual_periods=annual_periods)


def qlib_walk_forward(df: pd.DataFrame, settings: Settings, state_dir: str | Path,
                      *, annual_periods: int = 252) -> dict:
    """Same forward test windows and max exposure as A/B/C. No test-based selection."""
    from dataclasses import replace
    df = df.reset_index(drop=True)
    quality = audit_dataset_snapshot(df)
    folds = []
    oos_paths = []
    regime_tags = trailing_regimes(df)
    groups = ("A_baseline", "Q_qlib_lgbmodel", "buy_hold_same_exposure")
    for i, (train, val, test) in enumerate(walk_forward_windows(len(df)), 1):
        pool = _baseline_pool(df, train)
        name, _, a_validation = _best_on_validation(
            pool, df, train, val, settings, annual_periods
        )
        q_train_signal = qlib_signals(df, train, state_dir)
        q_validation = _score(df, q_train_signal, train, val, settings, annual_periods)
        # Refit using all known pre-test information (no test labels).
        a_test_signal = _test_signal(df, name, train, val, [])
        q_test_signal = qlib_signals(df, val, state_dir)
        fixed_buy_hold = np.ones(len(df), dtype=float)
        signals_by_group = {
            "A_baseline": a_test_signal,
            "Q_qlib_lgbmodel": q_test_signal,
            "buy_hold_same_exposure": fixed_buy_hold,
        }
        results = {}
        stress_settings = replace(settings, fee_bps=settings.fee_bps * 2,
                                  slippage_bps=settings.slippage_bps * 2)
        paths = {}
        for group, signal in signals_by_group.items():
            path = daily_net_returns(df, signal, val, test, settings)
            paths[group] = path
            results[group] = {
                "validation": a_validation if group == "A_baseline"
                else q_validation if group == "Q_qlib_lgbmodel" else None,
                "test": _score(df, signal, val, test, settings, annual_periods),
                "test_stress_2x_cost": _score(
                    df, signal, val, test, stress_settings, annual_periods
                ),
            }
            if not np.isclose(np.prod(1 + path) - 1, results[group]["test"]["total_return"], rtol=1e-9, atol=1e-10):
                raise ValueError("Diagnostic path disagrees with backtest")
            results[group]["regime_attribution"] = regime_attribution(
                df, path, val, test, regimes=regime_tags
            )
        oos_paths.append(paths)
        results["A_baseline"]["strategy"] = name
        results["Q_qlib_lgbmodel"]["strategy"] = "Qlib LGBModel binary / 0.55 fixed"
        folds.append({
            "fold": i,
            "train_end_exclusive": df.iloc[train]["date"].date().isoformat(),
            "validation_end_exclusive": df.iloc[val]["date"].date().isoformat(),
            "test_end_exclusive": df.iloc[test]["date"].date().isoformat(),
            "results": results,
        })
    summaries = {}
    for group in groups:
        tests = [fold["results"][group]["test"] for fold in folds]
        stresses = [fold["results"][group]["test_stress_2x_cost"] for fold in folds]
        summaries[group] = {
            "compounded_return": float(np.prod([1 + r["total_return"] for r in tests]) - 1),
            "mean_fold_sharpe": float(np.mean([r["sharpe"] for r in tests])),
            "worst_fold_max_drawdown": float(min(r["max_drawdown"] for r in tests)),
            "orders": int(sum(r["orders"] for r in tests)),
            "compounded_return_2x_cost": float(np.prod(
                [1 + r["total_return"] for r in stresses]) - 1),
        }
    return {
        "engine": "qlib.contrib.model.gbdt.LGBModel",
        "study": "causal single-asset next-open binary direction (not Qlib Alpha158)",
        "data_quality": quality,
        "out_of_sample_only": True,
        "promotion_eligible": False,
        "real_orders_enabled": False,
        "folds": folds,
        "out_of_sample_summary": summaries,
        "diagnostics": diagnostic_summary(oos_paths),
    }

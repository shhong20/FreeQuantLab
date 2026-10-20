"""Exploratory, causally dated research diagnostics; NEVER a live-trade gate.

Daily P&L exactly reproduces quantlab.engine.simulate's next-open,
long/cash, cost model. Market regimes use trailing information known
at decision close, not future prices. Bootstrap resamples contiguous
paired daily OOS log-return differences, never trains/tunes on test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from .settings import Settings


def daily_net_returns(
    df: pd.DataFrame, signal: np.ndarray, start: int, end: int,
    settings: Settings, *, scale: float = 1.0,
) -> np.ndarray:
    """One daily factor-1 per decision bar t (fill open t+1; exit open t+2).

    This intentionally mirrors engine.simulate(), including the last
    forced-exit cost and the fee on actual changes of exposure.
    """
    if not 0 <= start < end <= len(df) - 2:
        raise ValueError("Invalid OOS interval")
    if scale not in (0, 0.5, 1):
        raise ValueError("Risk overlay must be 0, 0.5, or 1")
    raw = np.asarray(signal, dtype=float)
    if len(raw) != len(df) or not np.isin(raw[start:end], (0., 1.)).all():
        raise ValueError("Signals must be full-length and long/cash only")
    pos = raw[start:end] * settings.max_position_fraction * scale
    opening = df["open"].to_numpy(dtype=float)
    if not np.isfinite(opening[start+1:end+2]).all() or np.any(opening[start+1:end+2] <= 0):
        raise ValueError("Invalid opening prices")
    returns = opening[start+2:end+2] / opening[start+1:end+1] - 1.
    fees = (settings.fee_bps + settings.slippage_bps) / 10_000.
    changed = np.abs(np.diff(np.r_[0., pos]))
    factors = (1. - changed * fees) * (1. + pos * returns)
    if pos[-1] > 0:
        factors[-1] *= 1. - pos[-1] * fees
    if not np.isfinite(factors).all() or np.any(factors <= 0):
        raise ValueError("Nonpositive/invalid daily equity factors")
    return factors - 1.


def trailing_regimes(df: pd.DataFrame, *, lookback: int = 60) -> pd.DataFrame:
    """Market direction and volatility known at close t (decision index t).

    Momentum bands +/-5% on 60-bar trailing close return. Volatility is
    20-bar realized volatility versus the trailing 90-bar median of
    volatility (including the current completed close).
    """
    if not 20 <= lookback <= 252:
        raise ValueError("Invalid causal regime lookback")
    close = df["close"].astype(float)
    movement = close.pct_change(lookback, fill_method=None)
    direction = np.select(
        [movement >= .05, movement <= -.05],
        ["up", "down"], default="sideways",
    )
    r = close.pct_change(fill_method=None)
    vol20 = r.rolling(20, min_periods=20).std()
    prior_median = vol20.rolling(90, min_periods=40).median()
    volatility = np.where(vol20 > prior_median, "high", "low")
    valid = movement.notna() & vol20.notna() & prior_median.notna()
    return pd.DataFrame({
        "direction": np.where(valid, direction, "unknown"),
        "volatility": np.where(valid, volatility, "unknown"),
    }, index=df.index)


def regime_attribution(
    df: pd.DataFrame, daily: np.ndarray, start: int, end: int,
    *, regimes: pd.DataFrame | None = None,
) -> dict:
    """OOS diagnostic attribution: log equity contributions add across regimes.

    Do not interpret noncontiguous regime contributions as an independently
    investable, continuously compounded strategy.
    """
    path = np.asarray(daily, dtype=float)
    if len(path) != end - start or np.any(path <= -1) or not np.isfinite(path).all():
        raise ValueError("Invalid OOS daily net return path")
    tags = regimes if regimes is not None else trailing_regimes(df)
    result = {}
    for axis in ("direction", "volatility"):
        observed = tags.iloc[start:end][axis].to_numpy()
        result[axis] = {}
        for label in sorted(set(observed)):
            mask = observed == label
            vals = path[mask]
            result[axis][str(label)] = {
                "days": int(mask.sum()),
                "mean_daily_net_return": float(vals.mean()),
                "log_equity_contribution": float(np.log1p(vals).sum()),
                "positive_days": int(np.count_nonzero(vals > 0)),
            }
    return result


def screen_redundant_factors(
    proposals: Sequence, features: pd.DataFrame, train_end: int,
    *, near_duplicate_agreement: float = .98,
) -> tuple[list, list[dict]]:
    """Remove constant/duplicate binary alpha signals using TRAIN data ONLY.

    Any future changes, validation outcomes or test returns must NOT alter
    screening or the remaining order. This does not test expected profit.
    """
    if train_end <= 100 or train_end > len(features):
        raise ValueError("Insufficient training history")
    if not .5 < near_duplicate_agreement <= 1.:
        raise ValueError("Invalid duplicate threshold")
    kept, outcomes = [], []
    signals = []
    for candidate in proposals:
        sig = np.asarray(candidate.compute(features), dtype=float)
        if len(sig) != len(features) or not np.isin(sig, (0., 1.)).all():
            raise ValueError("Invalid factor signals")
        train_sig = sig[:train_end]
        active = float(train_sig.mean())
        reason = None
        if active < .02 or active > .98:
            reason = "nearly_constant_training_signal"
        else:
            for previous in signals:
                if np.mean(previous == train_sig) >= near_duplicate_agreement:
                    reason = "near_duplicate_training_signal"
                    break
        if reason is None:
            kept.append(candidate)
            signals.append(train_sig.copy())
        outcomes.append({
            "name": candidate.name,
            "status": "kept" if reason is None else "dropped",
            "reason": reason,
            "training_active_fraction": active,
        })
    return kept, outcomes


def paired_block_bootstrap(
    strategy_folds: Sequence[np.ndarray],
    benchmark_folds: Sequence[np.ndarray],
    *, block_size: int = 10, repetitions: int = 1000, seed: int = 42,
) -> dict:
    """Exploratory paired OOS log-relative-return bootstrap by fold.

    Moving blocks with circular wrap, sampled independently WITHIN each
    chronological fold. This captures some serial dependence, but it
    does not correct for selecting strategies after observing test data,
    multiple comparisons, model instability or other data snooping.
    """
    if not 2 <= block_size <= 60 or not 100 <= repetitions <= 10000:
        raise ValueError("Invalid block bootstrap settings")
    if not strategy_folds or len(strategy_folds) != len(benchmark_folds):
        raise ValueError("Paired OOS folds required")
    deltas = []
    for a, b in zip(strategy_folds, benchmark_folds):
        x, y = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
        if x.ndim != 1 or y.ndim != 1 or len(x) != len(y) or len(x) < 5:
            raise ValueError("Misaligned/insufficient OOS observations")
        if not np.isfinite(x).all() or not np.isfinite(y).all() or np.any(x <= -1) or np.any(y <= -1):
            raise ValueError("Invalid return observations")
        deltas.append(np.log1p(x) - np.log1p(y))
    observed = float(np.concatenate(deltas).sum())
    rng = np.random.default_rng(seed)
    samples = np.zeros(repetitions, dtype=float)
    for i in range(repetitions):
        total = 0.
        for delta in deltas:
            n = len(delta)
            chunks = (n + block_size - 1) // block_size
            starts = rng.integers(0, n, size=chunks)
            ix = (starts[:, None] + np.arange(block_size)) % n
            total += float(delta[ix.ravel()[:n]].sum())
        samples[i] = total
    low, high = np.quantile(samples, (.025, .975))
    return {
        "method": "fold_stratified_circular_moving_block_paired_log_returns",
        "comparison": "strategy_vs_benchmark",
        "observed_log_excess": observed,
        "bootstrap_95pct_interval_log_excess": [float(low), float(high)],
        "bootstrap_positive_fraction": float(np.mean(samples > 0)),
        "oos_days": int(sum(len(d) for d in deltas)),
        "fold_count": len(deltas),
        "block_size": block_size,
        "bootstrap_samples": repetitions,
        "seed": seed,
        "exploratory_only": True,
        "multiple_testing_corrected": False,
        "eligible_for_automatic_promotion": False,
    }


def diagnostic_summary(
    daily_groups_by_fold: Sequence[dict[str, np.ndarray]],
    *, benchmark: str = "buy_hold_same_exposure",
) -> dict:
    """Diagnose pre-specified strategies; never elect an OOS winner."""
    if not daily_groups_by_fold:
        raise ValueError("No OOS data")
    groups = list(daily_groups_by_fold[0])
    if benchmark not in groups:
        raise ValueError("Missing benchmark")
    if any(set(fold) != set(groups) for fold in daily_groups_by_fold):
        raise ValueError("Fold groups mismatch")
    paired = {}
    baseline = [f[benchmark] for f in daily_groups_by_fold]
    for name in groups:
        if name != benchmark:
            paired[name] = paired_block_bootstrap(
                [f[name] for f in daily_groups_by_fold], baseline
            )
    return {
        "benchmark": benchmark,
        "comparisons": paired,
        "validation_warning": (
            "Exploratory OOS diagnostics only; inspecting/comparing these "
            "results makes this period unsuitable for another model-selection "
            "round. Blocks are not a proof of tradable edge."
        ),
        "promotes_to_paper_or_live": False,
    }

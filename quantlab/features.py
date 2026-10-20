"""All features use information available at the end of each completed bar."""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_COLUMNS = ("r1", "r3", "r5", "r10", "ma10", "ma30", "vol10", "range1", "volume_ratio")


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"].astype(float)
    feat = pd.DataFrame(index=df.index)
    for k in (1, 3, 5, 10):
        feat[f"r{k}"] = close.pct_change(k, fill_method=None)
    feat["ma10"] = close / close.rolling(10).mean() - 1
    feat["ma30"] = close / close.rolling(30).mean() - 1
    feat["vol10"] = close.pct_change(fill_method=None).rolling(10).std()
    feat["range1"] = (df["high"] - df["low"]) / close
    feat["volume_ratio"] = df["volume"] / df["volume"].rolling(20).mean().replace(0, np.nan) - 1
    return feat.replace([np.inf, -np.inf], np.nan)


def forward_label(df: pd.DataFrame) -> pd.Series:
    """Signal at close t -> buy open t+1 -> sell/rebalance open t+2.

    The most recent TWO rows are always ineligible for supervised training.
    """
    next_open = df["open"].shift(-1)
    after_open = df["open"].shift(-2)
    result = (after_open / next_open) - 1
    return result.gt(0).where(result.notna())  # missing label stays missing

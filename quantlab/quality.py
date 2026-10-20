"""Fail-closed validation of BTC/ETH daily candle continuity and paper quotes.

This is an integrity gate, NOT an anomaly trading signal, yield forecast,
or a guarantee that market-provider data are accurate. For actively traded
Upbit KRW spot assets, missing UTC dates/huge discontinuities require review.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

import numpy as np
import pandas as pd


class MarketIntegrityError(ValueError):
    pass


def validate_shadow_market(
    frame: pd.DataFrame, now: datetime, *,
    lookback: int = 100,
    max_daily_ratio: float = 2.0,
) -> dict:
    if now.tzinfo is None:
        raise MarketIntegrityError("Timezone-aware clock is required")
    current = now.astimezone(timezone.utc)
    required = {"date","open","high","low","close","volume"}
    if not isinstance(frame,pd.DataFrame) or not required.issubset(frame.columns):
        raise MarketIntegrityError("Missing daily OHLCV columns")
    if len(frame) < lookback or lookback < 80:
        raise MarketIntegrityError("Insufficient daily history for strict validation")
    df = frame.iloc[-lookback:].copy()
    try:
        dates = pd.to_datetime(df["date"],errors="raise")
        dates = dates.dt.tz_localize(None).dt.normalize()
    except (TypeError,ValueError,AttributeError) as exc:
        raise MarketIntegrityError("Invalid daily timestamps") from exc
    if dates.isna().any() or dates.duplicated().any():
        raise MarketIntegrityError("Missing or duplicate UTC day")
    if not dates.is_monotonic_increasing or (
        dates.diff().iloc[1:].dt.days != 1
    ).any():
        raise MarketIntegrityError("Nonconsecutive or unordered UTC daily candles")
    expected = (current.date()-timedelta(days=1)).isoformat()
    last_day = dates.iloc[-1].date().isoformat()
    if last_day != expected:
        raise MarketIntegrityError("Latest completed UTC daily candle is stale or incomplete")
    price = df[["open","high","low","close"]].to_numpy(dtype=float)
    volume = df["volume"].to_numpy(dtype=float)
    if not np.isfinite(price).all() or not np.isfinite(volume).all():
        raise MarketIntegrityError("Nonfinite OHLCV values")
    if (price <= 0).any() or (volume < 0).any():
        raise MarketIntegrityError("Negative volume or nonpositive OHLC prices")
    if np.any(price[:,1] < price[:,[0,2,3]].max(axis=1)) or np.any(
        price[:,2] > price[:,[0,1,3]].min(axis=1)
    ):
        raise MarketIntegrityError("Contradictory candle OHLC bounds")
    if np.any(volume[-20:] <= 0):
        raise MarketIntegrityError("Zero-volume recent UTC daily candle")
    close = price[:,3]
    ratios = close[1:]/close[:-1]
    if np.any((ratios > max_daily_ratio) | (ratios < 1/max_daily_ratio)):
        raise MarketIntegrityError("Extreme daily close ratio requires manual source verification")
    return {
        "status":"PASS","completed_utc_day":last_day,"inspected_days":lookback,
        "last_completed_close":float(close[-1]),
        "daily_dates_consecutive":True,"recent_positive_volume":True,
        "real_orders_enabled":False,
        "caveat":"Fails closed for suspect data; does not prove source authenticity",
    }


def validate_public_quote(
    latest_close: float, quote_price: float, *,
    max_quote_ratio: float = 1.8,
) -> None:
    if not (math.isfinite(latest_close) and math.isfinite(quote_price)
            and latest_close > 0 and quote_price > 0):
        raise MarketIntegrityError("Invalid price or previous daily close")
    if quote_price > max_quote_ratio*latest_close or quote_price < latest_close/max_quote_ratio:
        raise MarketIntegrityError("Current ticker deviates excessively from previous completed close")


def audit_dataset_snapshot(frame: pd.DataFrame) -> dict:
    """Research-oriented structural quality audit; no UTC daily frequency assumption.

    Stock exchange calendars are not daily calendar days, so this audit merely
    checks ordered unique trading-session dates and valid OHLCV without
    applying cryptocurrency continuity rules to KRX or US equities.
    """
    cols=["date","open","high","low","close","volume"]
    if not all(c in frame for c in cols) or frame.empty:
        raise MarketIntegrityError("Incomplete research dataset")
    dates=pd.to_datetime(frame["date"],errors="raise")
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise MarketIntegrityError("Research session dates are missing/duplicated/unordered")
    numbers=frame[cols[1:]].to_numpy(dtype=float)
    if not np.isfinite(numbers).all() or (numbers[:,:4]<=0).any() or (numbers[:,4]<0).any():
        raise MarketIntegrityError("Research OHLCV contains invalid values")
    if (numbers[:,1]<numbers[:,[0,2,3]].max(axis=1)).any() or (
        numbers[:,2]>numbers[:,[0,1,3]].min(axis=1)
    ).any():
        raise MarketIntegrityError("Research OHLCV bounds inconsistent")
    return {"status":"PASS","rows":len(frame),"first_session":str(dates.iloc[0].date()),
            "last_session":str(dates.iloc[-1].date()),"real_orders_enabled":False}

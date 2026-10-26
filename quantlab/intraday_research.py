"""Isolated, read-only 5-minute equity strategy research. Never places orders.

Input is a point-in-time, completed-session CSV. Timestamps label bar START.
Signals use completed bars; entry is NEXT bar open and exit is final bar OPEN.
All results are research evidence, not forward paper or executable fills.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import math
from pathlib import Path
import re
import sys
from statistics import median
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


class IntradayDataError(ValueError):
    """Reject incomplete, contradictory, or temporally ambiguous intraday data."""


MARKETS = {
    "KRX_STOCK": ("Asia/Seoul", 9, 0, 78, 20.0),
    "KRX_ETF": ("Asia/Seoul", 9, 0, 78, 0.0),
    "US_EQUITY": ("America/New_York", 9, 30, 78, 0.0),
}
REQUIRED = ("timestamp", "open", "high", "low", "close", "volume")
PERIODS_PER_YEAR = 252


@dataclass(frozen=True)
class Strategy:
    family: str
    bars: int = 0
    threshold: float = 0.0

    @property
    def name(self) -> str:
        if self.family == "orb":
            return f"orb_{self.bars * 5}m_rvol1.5"
        if self.family == "gap_reversion":
            return f"gap_down_{self.threshold * 100:.2f}pct"
        return "intraday_momentum_30m"


STRATEGIES = (
    Strategy("orb", bars=1),
    Strategy("orb", bars=3),
    Strategy("orb", bars=6),
    Strategy("gap_reversion", threshold=0.0075),
    Strategy("gap_reversion", threshold=0.015),
    Strategy("intraday_momentum", bars=6, threshold=0.002),
)


def read_csv(path: str | Path, market: str) -> tuple[pd.DataFrame, str]:
    if market not in MARKETS:
        raise IntradayDataError("Unsupported market")
    blob = Path(path).read_bytes()
    if not blob:
        raise IntradayDataError("Empty input")
    try:
        frame = pd.read_csv(io.BytesIO(blob))
    except (ValueError, pd.errors.ParserError) as exc:
        raise IntradayDataError("Invalid 5-minute CSV") from exc
    return validate(frame, market), sha256(blob).hexdigest()


def validate(frame: pd.DataFrame, market: str) -> pd.DataFrame:
    """Require 78 contiguous, valid five-minute regular-session bars per supplied day.

    Holidays/missing *whole* days are not filled. Short US sessions and halted
    sessions require a separate reviewed calendar/data policy, so fail closed.
    """
    if market not in MARKETS:
        raise IntradayDataError("Unsupported market")
    if not set(REQUIRED).issubset(frame.columns) or frame.empty:
        raise IntradayDataError("Require timestamp,open,high,low,close,volume")
    df = frame.loc[:, REQUIRED].copy()
    texts = df["timestamp"].astype(str)
    if not texts.str.contains(r"(?:Z|[+-]\d{2}:\d{2})$", regex=True).all():
        raise IntradayDataError("Each timestamp must have a timezone offset")
    try:
        df["ts"] = pd.to_datetime(df["timestamp"], utc=True, errors="raise")
        for col in REQUIRED[1:]:
            df[col] = pd.to_numeric(df[col], errors="raise")
    except (ValueError, TypeError) as exc:
        raise IntradayDataError("Invalid timestamp or OHLCV") from exc
    if df["ts"].isna().any() or df[list(REQUIRED[1:])].isna().any().any():
        raise IntradayDataError("Missing bar values")
    if not df["ts"].is_monotonic_increasing or df["ts"].duplicated().any():
        raise IntradayDataError("Bars must have strictly increasing unique timestamps")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise IntradayDataError("Prices must be positive")
    if (df["volume"] < 0).any():
        raise IntradayDataError("Volume cannot be negative")
    if not np.isfinite(df[list(REQUIRED[1:])].to_numpy(dtype=float)).all():
        raise IntradayDataError("Nonfinite OHLCV")
    if (df["high"] < df[["open", "low", "close"]].max(axis=1)).any() or (
        df["low"] > df[["open", "high", "close"]].min(axis=1)
    ).any():
        raise IntradayDataError("Inconsistent OHLC")
    tz_name, hour, minute, bars, _ = MARKETS[market]
    local = df["ts"].dt.tz_convert(ZoneInfo(tz_name))
    df["session"] = local.dt.strftime("%Y-%m-%d")
    start = hour * 60 + minute
    # Explicit local wall-clock comparison detects gaps, duplicates and partial days.
    expected = [f"{(start + 5*i)//60:02d}:{(start + 5*i)%60:02d}"
                for i in range(bars)]
    wallclock = local.dt.strftime("%H:%M")
    for day, rows in df.groupby("session", sort=False):
        times = wallclock.loc[rows.index].tolist()
        if times != expected:
            raise IntradayDataError(
                f"Incomplete/non-contiguous regular session {day}; "
                "do not backfill missing bars or invent early close"
            )
    if len(df["session"].unique()) < 60:
        raise IntradayDataError("At least 60 complete past sessions required")
    now = pd.Timestamp(datetime.now(timezone.utc))
    if df["ts"].max() + pd.Timedelta(minutes=5) > now:
        raise IntradayDataError("Current or future session bars are not completed")
    return df.reset_index(drop=True)


def _trade(day: str, name: str, bars: pd.DataFrame, signal_bar: int,
           buy_fee_bps: float, sell_tax_bps: float) -> dict | None:
    """Bar N close signal enters OPEN N+1; exit last regular bar OPEN."""
    entry_bar = signal_bar + 1
    final_bar = len(bars) - 1
    if entry_bar >= final_bar:
        return None
    entry = float(bars.iloc[entry_bar]["open"])
    exit_price = float(bars.iloc[final_bar]["open"])
    buy = entry * (1.0 + buy_fee_bps / 10000.0)
    sell = exit_price * (1.0 - (buy_fee_bps + sell_tax_bps) / 10000.0)
    ret = sell / buy - 1.0
    return {
        "session": day, "strategy": name, "signal_timestamp": str(bars.iloc[signal_bar]["ts"]),
        "entry_timestamp": str(bars.iloc[entry_bar]["ts"]),
        "exit_timestamp": str(bars.iloc[final_bar]["ts"]),
        "entry_open": entry, "exit_open": exit_price,
        "net_return": ret,
    }


def build_trades(frame: pd.DataFrame, *, fee_bps: float = 3.0,
                 slippage_bps: float = 5.0,
                 sell_tax_bps: float = 0.0,
                 corporate_actions_reviewed: bool = False) -> dict[str, list[dict]]:
    """Build all candidate trades, using only past sessions for RVOL baselines."""
    if any(not math.isfinite(x) or x < 0 for x in (
        fee_bps, slippage_bps, sell_tax_bps
    )) or fee_bps + slippage_bps + sell_tax_bps >= 10000:
        raise IntradayDataError("Invalid costs")
    all_trades: dict[str, list[dict]] = {strategy.name: [] for strategy in STRATEGIES}
    previous_close: float | None = None
    opening_volumes: dict[int, list[float]] = {1: [], 3: [], 6: []}
    for day, bars in frame.groupby("session", sort=True):
        bars = bars.reset_index(drop=True)
        opening = float(bars.iloc[0]["open"])
        for strat in STRATEGIES:
            signal_bar = None
            if strat.family == "orb":
                n = strat.bars
                history = opening_volumes[n][-20:]
                opening_volume = float(bars.iloc[:n]["volume"].sum())
                # A minimum of 10 PRIOR sessions means today's volume is never
                # included in its own benchmark.
                if len(history) >= 10 and median(history) > 0 and (
                    opening_volume >= 1.5 * median(history)
                ):
                    range_high = float(bars.iloc[:n]["high"].max())
                    for i in range(n, len(bars) - 2):
                        if float(bars.iloc[i]["close"]) > range_high:
                            signal_bar = i
                            break
            elif strat.family == "gap_reversion":
                # Only long gap-down reversal; short selling is not assumed.
                # Vendor dividend/split adjustments must be reviewed separately.
                if corporate_actions_reviewed and previous_close is not None:
                    gap = opening / previous_close - 1.0
                    if (-0.20 < gap <= -strat.threshold
                        and float(bars.iloc[0]["close"]) > opening):
                        signal_bar = 0
            elif strat.family == "intraday_momentum":
                i = strat.bars - 1
                if float(bars.iloc[i]["close"]) / opening - 1.0 >= strat.threshold:
                    signal_bar = i
            if signal_bar is not None:
                trade = _trade(day, strat.name, bars, signal_bar,
                               fee_bps + slippage_bps, sell_tax_bps)
                if trade is not None:
                    all_trades[strat.name].append(trade)
        for n in opening_volumes:
            opening_volumes[n].append(float(bars.iloc[:n]["volume"].sum()))
        # Final completed 5-minute bar CLOSE: known only for following session.
        previous_close = float(bars.iloc[-1]["close"])
    return all_trades


def _metrics(days: list[str], trades: list[dict]) -> dict:
    included = [t for t in trades if t["session"] in set(days)]
    lookup = {t["session"]: t for t in included}
    if len(lookup) != len(included):
        raise IntradayDataError("Overlapping trades on a session")
    returns = np.array([lookup[d]["net_return"] if d in lookup else 0.0
                        for d in days], dtype=float)
    if not len(returns):
        raise IntradayDataError("Empty evaluation window")
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(np.r_[1.0, equity])[1:]
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    sharpe = float(returns.mean() / std * np.sqrt(PERIODS_PER_YEAR)) if std > 0 else None
    active = np.array([t["net_return"] for t in included], dtype=float)
    return {
        "sessions": len(days),
        "trades": len(lookup),
        "net_compounded_return": float(equity[-1] - 1.0),
        "sharpe_annualized_daily": sharpe,
        "max_drawdown": float((1.0 - equity / peak).max()),
        "win_rate": float((active > 0).mean()) if len(active) else None,
        "mean_trade_return": float(active.mean()) if len(active) else None,
    }


def evaluate(frame: pd.DataFrame, *, market: str, source_sha256: str,
             fee_bps: float = 3.0, slippage_bps: float = 5.0,
             sell_tax_bps: float | None = None,
             corporate_actions_reviewed: bool = False) -> dict:
    """Selection uses validation ONLY. Final test is assessed after selections freeze."""
    if market not in MARKETS:
        raise IntradayDataError("Unsupported market")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise IntradayDataError("Missing valid raw dataset SHA256")
    default_tax = MARKETS[market][4]
    tax = default_tax if sell_tax_bps is None else sell_tax_bps
    sessions = list(frame["session"].unique())
    if len(sessions) < 60:
        raise IntradayDataError("At least 60 completed sessions required")
    split_train = int(0.60 * len(sessions))
    split_val = int(0.80 * len(sessions))
    validation = sessions[split_train:split_val]
    test = sessions[split_val:]
    trades = build_trades(frame, fee_bps=fee_bps, slippage_bps=slippage_bps,
                          sell_tax_bps=tax,
                          corporate_actions_reviewed=corporate_actions_reviewed)
    candidates = {}
    selected = {}
    final = {}
    for family in ("orb", "gap_reversion", "intraday_momentum"):
        group = [s for s in STRATEGIES if s.family == family]
        for s in group:
            candidates[s.name] = _metrics(validation, trades[s.name])
        eligible = [s for s in group if candidates[s.name]["trades"] >= 3]
        if not eligible:
            selected[family] = None
            final[family] = {"status": "NO_VALIDATION_CANDIDATE"}
            continue
        # Fix selection before any test metrics are read; ties deterministic.
        best = max(eligible, key=lambda s: (
            candidates[s.name]["net_compounded_return"], -group.index(s)
        ))
        selected[family] = best.name
        m = _metrics(test, trades[best.name])
        m["status"] = ("DIAGNOSTIC_ONLY_INSUFFICIENT_TEST_TRADES"
                       if m["trades"] < 10 else "RESEARCH_OOS_ONLY")
        final[family] = m
    return {
        "status": "ISOLATED_INTRADAY_RESEARCH_ONLY_NOT_PAPER_ELIGIBLE",
        "market": market,
        "dataset_sha256": source_sha256,
        "timestamp_convention": "5min_bar_start_UTC_offset_required",
        "source_sessions": len(sessions),
        "start_session": sessions[0],
        "last_completed_session": sessions[-1],
        "windows": {"train": [sessions[0], sessions[split_train - 1]],
                    "validation": [validation[0], validation[-1]],
                    "untouched_test": [test[0], test[-1]]},
        "costs_bps": {"fee_each_side": fee_bps, "slippage_each_side": slippage_bps,
                      "sell_tax_only": tax},
        "corporate_actions_reviewed_by_operator": corporate_actions_reviewed,
        "gap_strategy_blocked_without_review": not corporate_actions_reviewed,
        "validation_candidates": candidates,
        "selected_by_validation_only": selected,
        "untouched_test_selected_only": final,
        "limitations": [
            "No brokerage fills, orderbook, borrow, market-impact or queue simulation",
            "No automatic exchange-calendar/corporate-action verification",
            "Only complete regular full-length sessions; short sessions rejected",
            "Single-symbol long/cash, one trade per day; not portfolio performance",
            "Results are historical research; forward and live orders remain disabled",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Independent read-only equity 5min strategy comparison")
    p.add_argument("--csv", required=True, help="Completed 5min timestamp/open/high/low/close/volume CSV")
    p.add_argument("--market", required=True, choices=sorted(MARKETS))
    p.add_argument("--fee-bps", type=float, default=3.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--sell-tax-bps", type=float,
                   help="Explicit override; defaults KRX stock=20, ETF/US=0")
    p.add_argument("--corporate-actions-reviewed", action="store_true",
                   help="Operator attests historical gap input independently reviewed")
    p.add_argument("--out", help="Optional isolated JSON output path")
    args = p.parse_args(argv)
    try:
        frame, digest = read_csv(args.csv, args.market)
        result = evaluate(
            frame, market=args.market, source_sha256=digest,
            fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
            sell_tax_bps=args.sell_tax_bps,
            corporate_actions_reviewed=args.corporate_actions_reviewed,
        )
        payload = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
        if args.out:
            destination = Path(args.out)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return 0
    except (IntradayDataError, ValueError, OSError) as exc:
        p.exit(2, f"FAIL CLOSED (no trading): {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

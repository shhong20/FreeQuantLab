"""Offline, isolated TradingAgents decision research. Never executes an order.

TradingAgents' own backtest rates directional decisions rather than modelling
fills. This adapter maps *exported* ratings to conservative long/cash signals,
then uses FreeQuantLab's unchanged next-open simulator for a paired comparison.

Historical TradingAgents outputs are retrospective: today's LLM/news cannot
prove the inputs were available at the historical analysis date. They must
never be promoted as clean OOS or forward evidence.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd

from .data import normalize
from .engine import Candidate, signals, simulate
from .settings import Settings


class TradingAgentsLabError(ValueError):
    pass


RATINGS = frozenset({"Buy", "Overweight", "Hold", "Underweight", "Sell"})
REQUIRED_DECISION_KEYS = frozenset({
    "ticker", "decision_date", "rating", "generated_at",
    "model_id", "config_sha256", "provenance_kind",
})
RESEARCH_ONLY = "ISOLATED_RETROSPECTIVE_RESEARCH_NOT_FORWARD_OR_PAPER_ELIGIBLE"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_candles(path: str | Path) -> tuple[pd.DataFrame, str]:
    """Reject ambiguous/deduplicated or unfinished source bars before normalization."""
    raw = Path(path).read_bytes()
    if not raw or len(raw) > 100_000_000:
        raise TradingAgentsLabError("Empty or oversized CSV snapshot")
    try:
        frame = pd.read_csv(io.BytesIO(raw), dtype={"date": str})
    except (ValueError, pd.errors.ParserError, UnicodeError) as exc:
        raise TradingAgentsLabError("Invalid CSV snapshot") from exc
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        raise TradingAgentsLabError("Missing daily OHLCV fields")
    dates = frame["date"]
    if dates.isna().any() or not dates.map(lambda x: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", x))).all():
        raise TradingAgentsLabError("Daily session date must be YYYY-MM-DD")
    if dates.duplicated().any():
        raise TradingAgentsLabError("Duplicate sessions cannot be silently normalized")
    try:
        normalized = normalize(frame)
    except (ValueError, TypeError) as exc:
        raise TradingAgentsLabError("Invalid OHLCV snapshot") from exc
    if len(normalized) != len(frame) or len(normalized) < 120:
        raise TradingAgentsLabError("Require >=120 verified, unique completed daily sessions")
    if normalized["date"].max().date() >= datetime.now(timezone.utc).date():
        raise TradingAgentsLabError("Current/future UTC date cannot be considered completed")
    return normalized, _sha(raw)


def load_decisions(path: str | Path, ticker: str, valid_dates: set[str]) -> tuple[dict[str, str], str, dict]:
    """Require a small explicit export contract; unsupported modes fail closed."""
    raw = Path(path).read_bytes()
    if not raw or len(raw) > 5_000_000:
        raise TradingAgentsLabError("Missing or oversized decision JSONL")
    decisions: dict[str, str] = {}
    models: set[str] = set()
    configs: set[str] = set()
    for lineno, line in enumerate(raw.splitlines(), 1):
        try:
            item = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise TradingAgentsLabError(f"Invalid JSONL line {lineno}") from exc
        if not isinstance(item, dict) or set(item) != REQUIRED_DECISION_KEYS:
            raise TradingAgentsLabError(f"Invalid decision fields at line {lineno}")
        if item["ticker"] != ticker or item["provenance_kind"] != "retrospective":
            raise TradingAgentsLabError("Ticker mismatch or unverified forward provenance")
        date = item["decision_date"]
        if type(date) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or date not in valid_dates:
            raise TradingAgentsLabError("Decision outside frozen completed snapshot")
        if date in decisions:
            raise TradingAgentsLabError("Duplicate decision date")
        if item["rating"] not in RATINGS:
            raise TradingAgentsLabError("Unsupported/REVIEW decision (fail closed)")
        try:
            stamp = datetime.fromisoformat(item["generated_at"])
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("naive timestamp")
            session_day = datetime.strptime(date, "%Y-%m-%d").date()
            if stamp.date() < session_day:
                raise ValueError("generation precedes decision date")
        except (TypeError, ValueError) as exc:
            raise TradingAgentsLabError("Invalid generated_at provenance timestamp") from exc
        mid, cfg = item["model_id"], item["config_sha256"]
        if not isinstance(mid, str) or not 1 <= len(mid) <= 128:
            raise TradingAgentsLabError("Missing/invalid model ID")
        if not isinstance(cfg, str) or not SHA256_RE.fullmatch(cfg):
            raise TradingAgentsLabError("Missing/invalid config SHA256")
        decisions[date] = item["rating"]
        models.add(mid)
        configs.add(cfg)
    if not decisions:
        raise TradingAgentsLabError("No valid decisions")
    return decisions, _sha(raw), {"models": sorted(models), "config_sha256": sorted(configs), "count": len(decisions)}


def signal_from_ratings(
    dates: pd.Series, decisions: dict[str, str], *, max_age_sessions: int = 5,
) -> np.ndarray:
    """Hold is no new trade; unobserved decisions expire to cash, never short."""
    if not 1 <= max_age_sessions <= 20:
        raise TradingAgentsLabError("max_age_sessions out of range")
    signal = np.zeros(len(dates), dtype=float)
    exposure = 0.0
    last_decision_idx: int | None = None
    for index, date in enumerate(dates):
        key = date.strftime("%Y-%m-%d")
        if key in decisions:
            rating = decisions[key]
            if rating not in RATINGS:
                raise TradingAgentsLabError("Unsupported rating")
            last_decision_idx = index
            if rating in ("Buy", "Overweight"):
                exposure = 1.0
            elif rating in ("Underweight", "Sell"):
                exposure = 0.0
            # Hold preserves the earlier long/cash position inside the window.
        if last_decision_idx is None or index - last_decision_idx >= max_age_sessions:
            exposure = 0.0
        signal[index] = exposure
    return signal


def evaluate(
    df: pd.DataFrame, *,
    ticker: str, single: dict[str, str], multi: dict[str, str],
    settings: Settings = Settings(), max_age_sessions: int = 5,
) -> dict:
    """A fixed MA baseline, single-agent, multi-agent, and MA+multi risk veto."""
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.^_-]{0,24}", ticker):
        raise TradingAgentsLabError("Invalid ticker")
    if len(df) < 120 or not {"date", "open", "close"}.issubset(df.columns):
        raise TradingAgentsLabError("Insufficient daily OHLCV")
    dates = df["date"]
    valid_dates = set(dates.dt.strftime("%Y-%m-%d"))
    if not set(single).issubset(valid_dates) or not set(multi).issubset(valid_dates):
        raise TradingAgentsLabError("Decisions do not match price snapshot")
    start = int(len(df) * 0.8)
    end = len(df) - 2  # leave two actual open bars for exit PnL
    if end - start < 20:
        raise TradingAgentsLabError("Insufficient research evaluation period")
    baseline = signals(df, Candidate("momentum", 10, 40), start)
    single_sig = signal_from_ratings(dates, single, max_age_sessions=max_age_sessions)
    multi_sig = signal_from_ratings(dates, multi, max_age_sessions=max_age_sessions)
    if not any(x >= dates.iloc[start].strftime("%Y-%m-%d") for x in single):
        raise TradingAgentsLabError("Single-agent export has no evaluation-window decisions")
    if not any(x >= dates.iloc[start].strftime("%Y-%m-%d") for x in multi):
        raise TradingAgentsLabError("Multi-agent export has no evaluation-window decisions")
    lanes = {
        "A_fixed_MA10_40": baseline,
        "B_single_agent": single_sig,
        "C_multi_agent": multi_sig,
        "D_MA_with_multi_veto": baseline * multi_sig,
    }
    outputs = {}
    for name, signal in lanes.items():
        result = simulate(df, signal, start, end, settings)
        costly = simulate(df, signal, start, end, replace(
            settings, fee_bps=settings.fee_bps * 2, slippage_bps=settings.slippage_bps * 2,
        ))
        outputs[name] = {
            **result, "stress_2x_cost": costly,
            "active_signal_sessions": int(np.count_nonzero(signal[start:end])),
        }
    return {
        "status": RESEARCH_ONLY,
        "ticker": ticker,
        "comparison": outputs,
        "evaluation_window": {
            "decision_start": dates.iloc[start].date().isoformat(),
            "decision_end": dates.iloc[end - 1].date().isoformat(),
            "sessions": end - start,
        },
        "assumptions": {
            "max_age_sessions": max_age_sessions,
            "buy_overweight": "long_cap",
            "sell_underweight": "cash",
            "hold": "keep_prior_position_until_expiry",
            "missing_or_expired": "cash",
            "execution": "next_open_FQL_existing_simulator",
            "fee_bps": settings.fee_bps,
            "slippage_bps": settings.slippage_bps,
            "max_position_fraction": settings.max_position_fraction,
        },
        "lookahead_verified": False,
        "forward_eligible": False,
        "paper_eligible": False,
        "real_orders_enabled": False,
        "disclosure": (
            "Both LLM decision streams were created retrospectively; published news,"
            " social inputs, selected tickers and model sampling are not causally archived."
            " This is an engineering/research comparison only, not an OOS return claim."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated TradingAgents JSONL comparison; NO orders")
    parser.add_argument("--csv", required=True, type=Path, help="Frozen completed daily OHLCV")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--single", required=True, type=Path, help="Single-agent exported ratings JSONL")
    parser.add_argument("--multi", required=True, type=Path, help="TradingAgents exported ratings JSONL")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-age-sessions", type=int, default=5)
    parser.add_argument("--fee-bps", type=float, default=10.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args(argv)
    # No write to operating state, simulated fills, production reports or external locations.
    repo = Path(__file__).resolve().parents[1]
    allowed = (repo / "reports" / "tradingagents-lab").resolve()
    target = args.out.resolve()
    if target.parent != allowed or target.suffix != ".json":
        parser.error(f"--out must be a .json directly under {allowed}")
    if target.exists():
        parser.error("Immutable run: output already exists")
    try:
        df, csv_sha = load_candles(args.csv)
        valid = set(df["date"].dt.strftime("%Y-%m-%d"))
        single, single_sha, single_meta = load_decisions(args.single, args.ticker, valid)
        multi, multi_sha, multi_meta = load_decisions(args.multi, args.ticker, valid)
        data = evaluate(
            df, ticker=args.ticker, single=single, multi=multi,
            settings=Settings(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps),
            max_age_sessions=args.max_age_sessions,
        )
    except (TradingAgentsLabError, ValueError, OSError) as exc:
        parser.error(str(exc))
    data["input_sha256"] = {
        "daily_csv": csv_sha, "single_jsonl": single_sha, "multi_jsonl": multi_sha,
    }
    data["decision_sources"] = {"B": single_meta, "C": multi_meta}
    data["mode"] = "offline_export_only_no_model_invocations_no_network_or_broker"
    allowed.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids overwriting a prior decision report.
    with target.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"status": data["status"], "report": str(target),
                      "evaluation_window": data["evaluation_window"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Exploratory, reproducible, strictly chronological fixed-strategy stress tests.

No broker orders or simulated fill database writes. Models are refit on each
training prefix, then evaluated once on a non-overlapping future OOS fold.
Comparing these many folds is exploratory: previously inspected history and
researcher selection make it unsuitable as proof of independent alpha.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pandas as pd

from .data import upbit_daily, normalize
from .diagnostics import daily_net_returns, exploratory_familywise_max_t
from .engine import Candidate, signals, simulate
from .features import build_features
from .settings import Settings
from .daily_receipt import _safe_report

ASSETS = ("KRW-BTC", "KRW-ETH")
METHODS = (
    "buy_hold_20pct", "ma20_80", "ma10_40",
    "factor_r5_gt0", "logit_p60", "lgbm_p55",
)
COST_BPS = (15, 30, 60)
INITIAL_TRAIN_BARS = 600
FOLD_BARS = 120
MIN_FINAL_FOLD_BARS = 60


def verify_input(frame: pd.DataFrame) -> pd.DataFrame:
    # normalize() intentionally de-duplicates generic provider output.
    # For research provenance, reject duplicates/reordering *before* it can
    # silently repair a damaged historical input snapshot.
    raw_dates = pd.to_datetime(frame["date"], errors="raise")
    if raw_dates.duplicated().any() or not raw_dates.is_monotonic_increasing:
        raise ValueError("Duplicate or reordered exchange UTC days")
    df = normalize(frame)
    if len(df) < INITIAL_TRAIN_BARS + MIN_FINAL_FOLD_BARS + 2:
        raise ValueError("Too few genuinely closed bars for chronological stress test")
    dates = pd.to_datetime(df["date"])
    if (dates.diff().iloc[1:].dt.days != 1).any():
        raise ValueError("Missing calendar days; no invented OHLCV bars permitted")
    return df


def compare_frozen_archive(df: pd.DataFrame, symbol: str, archive: str | Path) -> dict:
    """Historical snapshot cannot silently disagree with sealed local OHLCV."""
    path = Path(archive)
    if not path.exists():
        return {"status": "NOT_AVAILABLE", "compared_days": 0}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as con:
        rows = pd.read_sql_query(
            "SELECT utc_day,open,high,low,close,volume FROM archive_candles "
            "WHERE symbol=? ORDER BY utc_day", con, params=(symbol,),
        )
    if rows.empty:
        raise ValueError("Archive has no records for " + symbol)
    baseline = df.assign(utc_day=df["date"].dt.strftime("%Y-%m-%d"))
    merged = baseline.merge(rows, on="utc_day", suffixes=("_source", "_archive"))
    if len(merged) != len(rows):
        raise ValueError("Fresh market history does not cover every archived day")
    for column in ("open", "high", "low", "close", "volume"):
        if not np.allclose(
            merged[column + "_source"], merged[column + "_archive"],
            rtol=1e-10, atol=1e-7,
        ):
            raise ValueError("Market history changed in stored archive: " + column)
    return {"status": "PASS", "compared_days": len(rows),
            "frozen_archive_last_day": rows["utc_day"].iloc[-1]}


def _signals(df: pd.DataFrame, train_end: int, *, include_ml: bool) -> dict[str, np.ndarray]:
    n = len(df)
    f = build_features(df)
    output = {
        "buy_hold_20pct": np.ones(n, dtype=float),
        "ma20_80": signals(df, Candidate("momentum", fast=20, slow=80), train_end),
        "ma10_40": signals(df, Candidate("momentum", fast=10, slow=40), train_end),
        "factor_r5_gt0": (f["r5"].gt(0) & f["r5"].notna()).astype(float).to_numpy(),
    }
    if include_ml:
        output["logit_p60"] = signals(df, Candidate("logit", threshold=.60), train_end)
        output["lgbm_p55"] = signals(df, Candidate("lgbm", threshold=.55), train_end)
    return output


def evaluate(
    frame: pd.DataFrame, *,
    initial_train: int = INITIAL_TRAIN_BARS,
    fold_bars: int = FOLD_BARS,
    include_ml: bool = True,
    max_t_reps: int = 300,
) -> dict:
    df = verify_input(frame)
    if initial_train < 300 or fold_bars < 60 or len(df) - 2 - initial_train < 60:
        raise ValueError("Training/test horizon too small")
    settings = Settings()
    records = []
    audit_groups: list[dict[str, np.ndarray]] = []
    start = initial_train
    fold = 0
    while start + MIN_FINAL_FOLD_BARS <= len(df) - 2:
        end = min(start + fold_bars, len(df) - 2)
        # Before predicting a new fold, train only on the historical prefix.
        signal_set = _signals(df, start, include_ml=include_ml)
        snapshot: dict[str,np.ndarray] = {}
        for name, signal in signal_set.items():
            for cost_bps in COST_BPS:
                scenario = replace(settings, fee_bps=10.0, slippage_bps=float(cost_bps-10))
                perf = simulate(df, signal, start, end, scenario, annual_periods=365)
                daily = daily_net_returns(df, signal, start, end, scenario)
                if not np.isclose(float(np.prod(1.0+daily)-1.0),perf["total_return"], atol=1e-8):
                    raise ValueError("Daily profit path disagrees with engine.simulate")
                if cost_bps == COST_BPS[0]:
                    snapshot[name] = daily
                records.append({
                    "fold": fold, "name": name, "cost_bps_per_side":cost_bps,
                    "training_closed_through": df["date"].iloc[start-1].date().isoformat(),
                    "decision_first": df["date"].iloc[start].date().isoformat(),
                    "decision_last": df["date"].iloc[end-1].date().isoformat(),
                    "oos_days":end-start,
                    "total_return": perf["total_return"], "max_drawdown": perf["max_drawdown"],
                    "sharpe": perf["sharpe"], "orders":perf["orders"],
                    "active_days":perf["active_days"],
                })
        audit_groups.append(snapshot)
        start = end
        fold += 1
    if not audit_groups:
        raise ValueError("No valid chronological OOS folds")
    names = list(audit_groups[0])
    benchmark = "buy_hold_20pct"
    summaries = []
    for name in names:
        for cost in COST_BPS:
            rows = [x for x in records if x["name"] == name
                    and x["cost_bps_per_side"] == cost]
            baselines = {x["fold"]:x for x in records if x["name"] == benchmark
                         and x["cost_bps_per_side"] == cost}
            log_growth = sum(np.log1p(x["total_return"]) for x in rows)
            log_excess = sum(
                np.log1p(x["total_return"]) - np.log1p(baselines[x["fold"]]["total_return"])
                for x in rows
            )
            summaries.append({
                "name":name, "cost_bps":cost,
                "oos_folds":len(rows), "oos_days":sum(x["oos_days"] for x in rows),
                "compounded_isolated_fold_return":float(np.expm1(log_growth)),
                "log_excess_vs_20pct_buy_hold":float(log_excess),
                "positive_excess_fold_fraction":float(np.mean([
                    x["total_return"] > baselines[x["fold"]]["total_return"] for x in rows
                ])),
                "positive_absolute_fold_fraction":float(np.mean([
                    x["total_return"] > 0 for x in rows
                ])),
                "median_oos_fold_return":float(np.median([x["total_return"] for x in rows])),
                "worst_fold_return":float(min(x["total_return"] for x in rows)),
                "worst_fold_max_drawdown":float(min(x["max_drawdown"] for x in rows)),
                "total_simulated_orders":sum(x["orders"] for x in rows),
            })
    family = [n for n in names if n != benchmark]
    max_t = exploratory_familywise_max_t(
        audit_groups, benchmark=benchmark, candidates=family, repetitions=max_t_reps,
    )
    return {
        "status": "EXPLORATORY_NOT_INDEPENDENT_ALPHA_PROOF",
        "dataset": {
            "bars":len(df), "first_utc":df["date"].iloc[0].date().isoformat(),
            "last_closed_utc":df["date"].iloc[-1].date().isoformat(),
        },
        "protocol": {
            "train_initial_bars":initial_train, "oos_fold_size":fold_bars,
            "oos_folds":fold, "non_overlapping_oos":True,
            "ml_refit_on_training_prefix_each_fold":include_ml,
            "label_maturity_gap_in_model":"engine._model uses train_end-2",
            "fill_assumption":"decision close t, execute next daily open t+1, hold until open t+2",
            "fees_slippage_bps":list(COST_BPS),
            "no_funding_tax_orderbook_liquidity":True,
            "isolation":"Each fold assumes flat opening and forced final exit",
            "not_true_exaone_historical_replay":True,
            "lightgbm_retrained_not_original_qlib_checkpoint":True,
            "candidate_family_includes_prior_research_choices":True,
            "past_dates_already_inspected_so_not_pristine_holdout":True,
        },
        "folds":records,
        "summary":summaries,
        "multiple_testing_sensitivity":max_t,
        "real_orders_enabled":False,
        "automatic_strategy_promotion":False,
    }


def run_long(
    report_dir: str | Path, archive: str | Path,
    *, bars:int = 3000,
) -> dict:
    root=Path(report_dir)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Report directory must be empty: never overwrite a past evaluation")
    root.mkdir(parents=True,exist_ok=True)
    result={"kind":"two_asset_historical_walkforward_exploratory",
            "captured_at_utc":datetime.now(timezone.utc).isoformat(),
            "datasets":{}, "results":{}, "issues":[],
            "real_orders_enabled":False, "automatic_strategy_promotion":False}
    for symbol in ASSETS:
        frame=verify_input(upbit_daily(symbol,bars))
        overlap=compare_frozen_archive(frame,symbol,archive)
        csv = frame.to_csv(index=False).encode("utf-8")
        filename=f"{symbol.replace('-', '_')}.csv"
        (root/filename).write_bytes(csv)
        result["datasets"][symbol]={
            "source":"Upbit public historical UTC daily OHLCV",
            "filename":filename, "sha256":hashlib.sha256(csv).hexdigest(),
            "archive_consistency":overlap,
            "rows":len(frame),
        }
        result["results"][symbol]=evaluate(frame)
    _safe_report(root/"results.json",result)
    return result


def run_snapshot(report_dir: str | Path, snapshot_dir: str | Path,
                 archive: str | Path) -> dict:
    """Repeat research on byte-identical stored candles without querying exchange."""
    source=Path(snapshot_dir)
    manifest=json.loads((source/"results.json").read_text())
    root=Path(report_dir)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Report directory must be empty")
    result={"kind":"two_asset_historical_walkforward_exploratory",
            "captured_at_utc":datetime.now(timezone.utc).isoformat(),
            "replayed_from_sha256_manifest":str(source/"results.json"),
            "datasets":{}, "results":{}, "issues":[],
            "real_orders_enabled":False,"automatic_strategy_promotion":False}
    for symbol in ASSETS:
        info=manifest["datasets"][symbol]
        raw=(source/info["filename"]).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=info["sha256"]:
            raise ValueError("Historical snapshot SHA-256 mismatch for "+symbol)
        frame=verify_input(pd.read_csv(source/info["filename"]))
        overlap=compare_frozen_archive(frame,symbol,archive)
        result["datasets"][symbol]=dict(info,archive_consistency=overlap)
        result["results"][symbol]=evaluate(frame)
    root.mkdir(parents=True,exist_ok=True)
    _safe_report(root/"results.json",result)
    return result


def main(argv=None)->int:
    p=argparse.ArgumentParser(description="Read-only chronological historical stress research")
    p.add_argument("--report-dir",default="reports/historical-stress-20261021")
    p.add_argument("--archive",default="/home/hong/apps/FreeQuantLab/state/market_history.db")
    p.add_argument("--bars",type=int,default=3000)
    p.add_argument("--snapshot-dir",help="Byte-hashed earlier results directory; reuse CSVs instead of network")
    args=p.parse_args(argv)
    try:
        result=(run_snapshot(args.report_dir,args.snapshot_dir,args.archive)
                if args.snapshot_dir else run_long(args.report_dir,args.archive,bars=args.bars))
        print(json.dumps({
            "kind":result["kind"],
            "report":str(Path(args.report_dir)/"results.json"),
            "datasets":result["datasets"],
            "strategies":{
                symbol:[{k:x[k] for k in ("name","cost_bps","oos_folds","oos_days",
                                          "compounded_isolated_fold_return",
                                          "log_excess_vs_20pct_buy_hold",
                                          "positive_excess_fold_fraction",
                                          "worst_fold_max_drawdown")}
                       for x in blob["summary"] if x["cost_bps"]==15]
                for symbol,blob in result["results"].items()
            },
            "real_orders_enabled":False,
        },ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError,RuntimeError,sqlite3.Error) as exc:
        print("HISTORICAL STRESS FAILED CLOSED: "+type(exc).__name__+": "+str(exc)[:250],file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

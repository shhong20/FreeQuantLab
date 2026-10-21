"""Actual Microsoft Qlib LGBModel historical stress with causal refits.

Exploratory only. Training labels mature before each non-overlapping test fold.
A predeclared hysteresis policy is evaluated alongside fixed threshold 0.55
without optimizing on OOS outcomes. This DOES NOT replay the October frozen
model, and does not place or synthesize broker/paper ledger orders.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pandas as pd

from .daily_receipt import _safe_report
from .diagnostics import daily_net_returns, exploratory_familywise_max_t
from .engine import simulate
from .historical_stress import (
    ASSETS, COST_BPS, INITIAL_TRAIN_BARS, FOLD_BARS,
    MIN_FINAL_FOLD_BARS, compare_frozen_archive, verify_input,
)
from .qlib_bridge import qlib_probabilities
from .settings import Settings

BENCHMARK = "buy_hold_20pct"
BASELINE = "qlib_055"
HYSTERESIS = "qlib_hysteresis_060_045"
PAPER_ONLY = True


def fixed_policies(prob: np.ndarray, start: int, end: int) -> dict[str, np.ndarray]:
    """Use only predictions known by each decision close; reset fold inventory.

    Given an open hypothetical position, exit on p < .45. Given zero
    exposure, enter on p >= .60. Equal cases do not trigger an exit.
    No test-label access or look-ahead rolling smoothing is allowed.
    """
    source = np.asarray(prob, dtype=float)
    if source.ndim != 1 or not 0 <= start < end <= len(source):
        raise ValueError("Invalid Qlib probability interval")
    used = source[start:end]
    if not np.isfinite(used).all() or np.any((used < 0) | (used > 1)):
        raise ValueError("Missing or invalid causal Qlib model probabilities")
    raw = np.zeros_like(source)
    hold = np.zeros_like(source)
    raw[start:end] = (used >= .55).astype(float)
    open_position = False
    for i in range(start, end):
        p = float(source[i])
        if not open_position and p >= .60:
            open_position = True
        elif open_position and p < .45:
            open_position = False
        hold[i] = float(open_position)
    return {BASELINE:raw, HYSTERESIS:hold}


def analyze(
    frame: pd.DataFrame, *,
    state_dir: str | Path,
    max_folds: int | None = None,
    predictor=qlib_probabilities,
    block_repetitions: int = 300,
) -> dict:
    df = verify_input(frame)
    if max_folds is not None and (type(max_folds) is not int or max_folds < 1):
        raise ValueError("max_folds must be positive")
    settings = Settings()
    n = len(df)
    records: list[dict] = []
    groups: list[dict[str,np.ndarray]] = []
    training_evidence = []
    fold_id = 0
    start = INITIAL_TRAIN_BARS
    while start + MIN_FINAL_FOLD_BARS <= n - 2:
        if max_folds is not None and fold_id >= max_folds:
            break
        end = min(start + FOLD_BARS, n - 2)
        # The actual Qlib implementation performs a further chronological
        # inner train/validation split with a two-bar label maturity embargo.
        # Future test candles are inference inputs only and never train labels.
        probs = np.asarray(predictor(df, start, state_dir), dtype=float)
        if len(probs) != n:
            raise ValueError("Qlib predictor returned unexpected probability length")
        selected = fixed_policies(probs, start, end)
        selected[BENCHMARK] = np.ones(n, dtype=float)
        paths: dict[str,np.ndarray] = {}
        for name, signal in selected.items():
            for cost in COST_BPS:
                scenario = replace(settings, fee_bps=10., slippage_bps=float(cost - 10))
                stats = simulate(df, signal, start, end, scenario, annual_periods=365)
                daily = daily_net_returns(df, signal, start, end, scenario)
                if not np.isclose(np.prod(1 + daily)-1, stats["total_return"], atol=1e-9):
                    raise ValueError("Qlib historical simulation paths disagree")
                if cost == COST_BPS[0]:
                    paths[name] = daily
                records.append({
                    "fold":fold_id,
                    "name":name,
                    "cost_bps_per_side":cost,
                    "trained_on_closed_candles_through":df["date"].iloc[start-1].date().isoformat(),
                    "oos_first_decision":df["date"].iloc[start].date().isoformat(),
                    "oos_last_decision":df["date"].iloc[end-1].date().isoformat(),
                    "days":stats["days"],
                    "total_return":stats["total_return"],
                    "max_drawdown":stats["max_drawdown"],
                    "orders":stats["orders"],
                    "active_days":stats["active_days"],
                })
        groups.append(paths)
        training_evidence.append({
            "fold":fold_id,
            "fit_end_exclusive_index":start,
            "last_training_label_decision_max_index":start-3,
            "feature_set_last_decision_index":end-1,
            "decision_first":df["date"].iloc[start].date().isoformat(),
            "decision_last":df["date"].iloc[end-1].date().isoformat(),
        })
        fold_id += 1
        start = end
    if not groups:
        raise ValueError("No OOS Qlib fold was evaluated")

    summary = []
    for name in (BENCHMARK,BASELINE,HYSTERESIS):
        for cost in COST_BPS:
            rows = [r for r in records if r["name"]==name and r["cost_bps_per_side"]==cost]
            benchmarks = {r["fold"]:r for r in records if
                          r["name"]==BENCHMARK and r["cost_bps_per_side"]==cost}
            summary.append({
                "name":name, "cost_bps":cost, "folds":len(rows),
                "oos_days":sum(r["days"] for r in rows),
                "compounded_isolated_fold_return":float(np.expm1(
                    sum(np.log1p(r["total_return"]) for r in rows)
                )),
                "log_excess_vs_20pct_buy_hold":float(sum(
                    np.log1p(r["total_return"])-np.log1p(benchmarks[r["fold"]]["total_return"])
                    for r in rows
                )),
                "positive_excess_fold_fraction":float(np.mean([
                    r["total_return"]>benchmarks[r["fold"]]["total_return"] for r in rows
                ])),
                "simulated_orders":sum(r["orders"] for r in rows),
                "worst_fold_max_drawdown":float(min(r["max_drawdown"] for r in rows)),
            })
    sensitivity=exploratory_familywise_max_t(
        groups,benchmark=BENCHMARK,candidates=[BASELINE,HYSTERESIS],
        repetitions=block_repetitions,seed=73,
    )
    return {
        "status":"EXPLORATORY_NO_CONFIRMED_ALPHA",
        "engine":"qlib.contrib.model.gbdt.LGBModel",
        "not_frozen_operational_checkpoint_replay":True,
        "not_qlib_alpha158":True,
        "dataset":{"bars":n,"first_utc":df["date"].iloc[0].date().isoformat(),
                   "last_closed_utc":df["date"].iloc[-1].date().isoformat()},
        "fixed_policies":{
            BASELINE:{"entry_and_exit_probability":.55},
            HYSTERESIS:{"enter_probability":.60,"exit_when_probability_below":.45},
        },
        "evaluation":{
            "max_train_label_decision_index":"fit_end_exclusive-3",
            "fit_initial_bars":INITIAL_TRAIN_BARS,
            "fold_bars":FOLD_BARS,
            "folds":fold_id,
            "causal_refits_only":True,
            "no_OOS_policy_tuning":True,
            "assumed_transaction_cost_bps_per_side":list(COST_BPS),
            "paper_price_model":"next_open_to_following_open; no executable quote or orderbook",
            "fold_initial_position_flat":True,
            "fold_forced_exit":True,
        },
        "training_boundaries":training_evidence,
        "fold_rows":records,
        "summary":summary,
        "exploratory_multiple_testing":sensitivity,
        "real_orders_enabled":False,
        "automatic_strategy_promotion":False,
    }


def evaluate_saved_snapshots(
    *, snapshot_dir: str | Path, report_dir: str | Path,
    archive: str | Path, state_dir: str | Path,
    max_folds: int | None = None,
    predictor=qlib_probabilities,
) -> dict:
    source=Path(snapshot_dir)
    manifest=json.loads((source/"results.json").read_text())
    target=Path(report_dir)
    if target.exists() and any(target.iterdir()):
        raise ValueError("Refuse to replace existing historical Qlib report")
    result={
        "kind":"actual_qlib_rolling_retrain_historical_stress",
        "captured_at_utc":datetime.now(timezone.utc).isoformat(),
        "source_results_file":str(source/"results.json"),
        "assets":{}, "real_orders_enabled":False,
    }
    for symbol in ASSETS:
        evidence=manifest["datasets"][symbol]
        name=evidence["filename"]
        if name != symbol.replace("-", "_") + ".csv":
            raise ValueError("Unexpected historical snapshot filename")
        blob=(source/name).read_bytes()
        if sha256(blob).hexdigest()!=evidence["sha256"]:
            raise ValueError("Historical snapshot byte hash mismatch for "+symbol)
        df=verify_input(pd.read_csv(source/name))
        overlap=compare_frozen_archive(df,symbol,archive)
        if overlap["status"]!="PASS":
            raise ValueError("Missing authoritative locally archived overlap: "+symbol)
        result["assets"][symbol]={
            "source_sha256":evidence["sha256"],
            "archived_overlap":overlap,
            "model":analyze(df,state_dir=state_dir,max_folds=max_folds,predictor=predictor),
        }
    target.mkdir(parents=True,exist_ok=True)
    _safe_report(target/"results.json",result)
    return result


def main(argv: list[str] | None=None) -> int:
    ap=argparse.ArgumentParser(description="Actual Qlib historical out-of-sample research")
    ap.add_argument("--snapshot-dir",default="reports/historical-stress-20261021")
    ap.add_argument("--report-dir",default="reports/qlib-causal-historical-20261021")
    ap.add_argument("--archive",default="/home/hong/apps/FreeQuantLab/state/market_history.db")
    ap.add_argument("--state-dir",default="state/qlib_historical_stress")
    ap.add_argument("--max-folds",type=int)
    args=ap.parse_args(argv)
    try:
        report=evaluate_saved_snapshots(
            snapshot_dir=args.snapshot_dir,report_dir=args.report_dir,
            archive=args.archive,state_dir=args.state_dir,max_folds=args.max_folds,
        )
        print(json.dumps({
            "status":"EXPLORATORY_NO_CONFIRMED_ALPHA",
            "report":str(Path(args.report_dir)/"results.json"),
            "assets":{
                symbol:[{k:r[k] for k in (
                    "name","cost_bps","folds","compounded_isolated_fold_return",
                    "simulated_orders","log_excess_vs_20pct_buy_hold",
                )} for r in evidence["model"]["summary"] if r["cost_bps"]==15]
                for symbol,evidence in report["assets"].items()
            },
            "real_orders_enabled":False,
        },ensure_ascii=False))
        return 0
    except (ValueError,OSError,RuntimeError,sqlite3.Error,ImportError) as exc:
        print("QLIB HISTORICAL FAIL CLOSED: "+type(exc).__name__+": "+str(exc)[:240],file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

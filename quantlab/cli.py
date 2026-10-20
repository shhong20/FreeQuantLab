from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import requests

from .data import DataError, fetch, live_paper_quote
from .engine import Candidate, research, signals
from .paper import paper_statistics, promotion_gate, record_paper
from .settings import load_config
from .agent_lab import AgentError, LocalLLMAgent, MockAgent, compare_agents


def asset_id(asset: dict) -> str:
    return f"{asset['provider']}__{asset.get('symbol', 'DEMO')}"


def safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def _asset(assets: list[dict], symbol: str) -> dict:
    matches = [a for a in assets if a.get("symbol") == symbol]
    if len(matches) != 1:
        raise ValueError("--symbol must identify exactly one configured asset")
    return matches[0]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _manifest_path(state_dir: Path, asset: dict) -> Path:
    return state_dir / "frozen" / (safe_filename(asset_id(asset)) + ".json")


def cmd_research(args: argparse.Namespace) -> int:
    settings, assets = load_config(args.config)
    if args.symbol:
        assets = [_asset(assets, args.symbol)]
    if not assets:
        raise ValueError("No configured assets")
    results = []
    for asset in assets:
        symbol = asset.get("symbol", "DEMO")
        try:
            frame = fetch(asset)
            result = research(frame, settings, annual_periods=365 if asset.get("kind") == "crypto" else 252)
            result.update({"symbol": symbol, "provider": asset["provider"], "currency": asset.get("currency"), "kind": asset.get("kind"), "bars": len(frame), "is_synthetic": asset["provider"] == "demo"})
            state = "[eligible for forward paper]" if result.get("status") == "PAPER_ELIGIBLE" else "[rejected]"
            print(f"{symbol} {state}: {result.get('strategy_id', '-')} / {result.get('reason')}")
        except (ValueError, KeyError, DataError) as exc:
            result = {"symbol": symbol, "provider": asset["provider"], "status": "ERROR", "error": str(exc)}
            print(f"{symbol} [data/evaluation error]: {exc}", file=sys.stderr)
        _write_json(Path(args.report_dir) / (safe_filename(asset_id(asset)) + ".json"), result)
        results.append(result)
    return 0 if all(r["status"] != "ERROR" for r in results) else 2


def cmd_agent_compare(args: argparse.Namespace) -> int:
    settings, assets = load_config(args.config)
    if args.symbol:
        assets = [_asset(assets, args.symbol)]
    if not assets:
        raise ValueError("No configured assets")
    if args.agent_mode == "mock":
        agent = MockAgent()
    else:
        agent = LocalLLMAgent(args.llm_url, args.llm_model)
    failed = False
    for asset in assets:
        name = asset.get("symbol", "DEMO")
        try:
            frame = fetch(asset)
            report = compare_agents(frame, settings, agent,
                                    annual_periods=365 if asset.get("kind") == "crypto" else 252,
                                    with_lightgbm=args.with_lightgbm)
            report.update({"provider": asset["provider"], "symbol": name,
                           "is_synthetic": asset["provider"] == "demo"})
            _write_json(Path(args.report_dir) / (safe_filename(asset_id(asset)) + "__agent_compare.json"), report)
            print(f"{name}: A/B/C + buy-hold on {len(report['folds'])} walk-forward folds; {report['agent_mode']}; research only, NO LIVE ORDERS")
            for k, v in report["out_of_sample_summary"].items():
                print(f"  {k}: return={v['compounded_return_across_four_nonoverlapping_test_windows']:.3%}, "
                      f"mean Sharpe={v['mean_fold_sharpe']:.2f}, orders={v['total_orders']}")
        except (AgentError, ValueError, DataError, KeyError, requests.RequestException) as exc:
            print(f"{name}: agent comparison FAIL CLOSED: {exc}", file=sys.stderr)
            failed = True
    return 2 if failed else 0


def cmd_freeze(args: argparse.Namespace) -> int:
    settings, assets = load_config(args.config)
    asset = _asset(assets, args.symbol)
    if asset["provider"] not in {"upbit", "kis"}:
        raise ValueError("Forward paper requires live Upbit or KIS quote; demo/csv/Stooq cannot freeze")
    df = fetch(asset)
    result = research(df, settings, annual_periods=365 if asset.get("kind") == "crypto" else 252)
    if result.get("status") != "PAPER_ELIGIBLE":
        print("REJECT: risk/research gate failed, no paper strategy frozen")
        return 3
    path = _manifest_path(Path(args.state_dir), asset)
    if path.exists():
        raise ValueError("Existing frozen paper strategy; preserving history. Explicitly archive before new research cycle")
    manifest = {"symbol": asset["symbol"], "provider": asset["provider"], "currency": asset["currency"],
                "kind": asset["kind"], "strategy_id": result["strategy_id"], "selected": result["selected"],
                "last_backtest_date": result["split_dates"]["last_completed"],
                "frozen_at": datetime.now(timezone.utc).isoformat(), "fee_bps": settings.fee_bps,
                "slippage_bps": settings.slippage_bps, "frozen_settings": asdict(settings), "research": result}
    _write_json(path, manifest)
    print(f"FROZEN for *forward paper only*: {path}")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    settings, assets = load_config(args.config)
    asset = _asset(assets, args.symbol)
    path = _manifest_path(Path(args.state_dir), asset)
    if not path.exists():
        raise ValueError("Freeze a proven strategy before forward paper (freequant freeze ...)")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if any(manifest[k] != asset[k] for k in ("provider", "symbol", "currency", "kind")):
        raise ValueError("Frozen strategy/account mismatch")
    if asdict(settings) != manifest["frozen_settings"]:
        raise ValueError("Settings changed since research; freeze state must be preserved")
    frame = fetch(asset)
    last = frame.iloc[-1]["date"].date().isoformat()
    if last <= manifest["last_backtest_date"]:
        print("SKIP: No genuinely new completed bar after strategy freeze")
        return 0
    c = Candidate(**manifest["selected"])
    sig = signals(frame, c, len(frame))
    decision = {"date": last, "target": int(sig[-1]), "strategy_id": manifest["strategy_id"]}
    price, session_date = live_paper_quote(asset["provider"], asset["symbol"])
    result = record_paper(args.db, provider=asset["provider"], symbol=asset["symbol"], currency=asset["currency"],
                          decision=decision, quote=price, session_date=session_date, settings=settings)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings, assets = load_config(args.config)
    items = assets if not args.symbol else [_asset(assets, args.symbol)]
    for asset in items:
        manifest_path = _manifest_path(Path(args.state_dir), asset)
        if not manifest_path.exists():
            print(f"{asset.get('symbol')} : no paper strategy frozen")
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stat = paper_statistics(args.db, asset["symbol"], manifest["strategy_id"])
        gate = promotion_gate(manifest["research"], stat, settings)
        print(json.dumps({"symbol": asset["symbol"], "paper": stat, "gate": gate}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FreeQuantLab: local AI research and forward paper, real orders OFF")
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--db", default="paper.db")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("research", "freeze", "paper", "status", "agent-compare"):
        p = sub.add_parser(name)
        p.add_argument("--symbol", required=name in ("freeze", "paper"))
        if name == "agent-compare":
            p.add_argument("--agent-mode", choices=["mock", "local"], default="local")
            p.add_argument("--llm-url", default="http://127.0.0.1:8000/v1")
            p.add_argument("--llm-model", default="local-model")
            p.add_argument("--with-lightgbm", action="store_true")
    args = parser.parse_args(argv)
    try:
        return {"research": cmd_research, "freeze": cmd_freeze,
                "paper": cmd_paper, "status": cmd_status,
                "agent-compare": cmd_agent_compare}[args.command](args)
    except (DataError, ValueError, KeyError, OSError) as exc:
        print(f"FAIL CLOSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

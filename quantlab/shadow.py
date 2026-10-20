"""Forward-only virtual cash ledger for pre-registered, immutable spot strategies.

Research incubation, NOT broker simulation parity, paper promotion or live orders.
All market quotes are read-only public Upbit endpoints. A frozen track cannot
trade until a NEW fully closed bar after its registration date has arrived.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import fcntl
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable

import numpy as np
import pandas as pd

from .data import fetch, live_paper_quote
from .features import build_features, FEATURE_COLUMNS
from .settings import Settings
from .quality import validate_shadow_market, validate_public_quote


TRACK_SPECS = (
    {"label": "buy_hold_20pct", "kind": "hold"},
    {"label": "trend_ma20_80", "kind": "momentum", "fast": 20, "slow": 80},
    {"label": "factor_r5_gt0", "kind": "factor", "feature": "r5",
     "direction": "gt", "threshold": 0.0},
)
RESEARCH_ONLY = True


def _utc(now: datetime | None = None) -> datetime:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Aware current time is required")
    return instant.astimezone(timezone.utc)


def _completed_utc_day(df: pd.DataFrame, now: datetime) -> str:
    if df.empty or len(df) < 100:
        raise ValueError("At least 100 completed bars required for monitoring")
    decision_date = pd.Timestamp(df.iloc[-1]["date"]).date()
    if decision_date != (now - timedelta(days=1)).date():
        # Fail closed on stale candles (or future/partially closed candles).
        raise ValueError("Most recent completed UTC daily candle must be yesterday")
    return decision_date.isoformat()


def _spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("Malformed shadow strategy")
    label, kind = spec.get("label"), spec.get("kind")
    if not isinstance(label, str) or not label.isascii() or not (
        1 <= len(label) <= 50 and all(c.isalnum() or c == "_" for c in label)
    ):
        raise ValueError("Invalid strategy label")
    if kind == "hold" and set(spec) == {"label", "kind"}:
        return dict(spec)
    if kind == "momentum" and set(spec) == {"label", "kind", "fast", "slow"}:
        fast, slow = spec["fast"], spec["slow"]
        if type(fast) is int and type(slow) is int and 2 <= fast < slow <= 180:
            return dict(spec)
    if kind == "factor" and set(spec) == {
        "label", "kind", "feature", "direction", "threshold"
    }:
        feature, direction, threshold = (
            spec["feature"], spec["direction"], spec["threshold"]
        )
        if (feature in FEATURE_COLUMNS and direction in ("gt", "lt")
            and type(threshold) in (float, int) and
                math.isfinite(threshold) and abs(threshold) <= .25):
            return dict(spec)
    raise ValueError("Unsupported immutable strategy specification")


def _target(frame: pd.DataFrame, spec: dict) -> int:
    spec = _spec(spec)
    if spec["kind"] == "hold":
        return 1
    if spec["kind"] == "momentum":
        c = frame["close"].astype(float)
        slow = c.rolling(spec["slow"], min_periods=spec["slow"]).mean().iloc[-1]
        fast = c.rolling(spec["fast"], min_periods=spec["fast"]).mean().iloc[-1]
        if not np.isfinite(slow) or not np.isfinite(fast):
            raise ValueError("Insufficient past bars for frozen trend")
        return int(fast > slow)
    feats = build_features(frame)
    value = float(feats.iloc[-1][spec["feature"]])
    if not np.isfinite(value):
        raise ValueError("Frozen factor feature is missing")
    return int(value > spec["threshold"] if spec["direction"] == "gt"
               else value < spec["threshold"])


def _connection(path: str | Path) -> sqlite3.Connection:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(dest, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS shadow_accounts (
      track_id TEXT PRIMARY KEY,
      provider TEXT NOT NULL, symbol TEXT NOT NULL, currency TEXT NOT NULL,
      label TEXT NOT NULL, spec_json TEXT NOT NULL, settings_json TEXT NOT NULL,
      registered_utc TEXT NOT NULL, frozen_bar TEXT NOT NULL,
      initial_cash REAL NOT NULL, cash REAL NOT NULL,
      units REAL NOT NULL, last_equity REAL NOT NULL,
      day_date TEXT, day_start_equity REAL NOT NULL,
      halted INTEGER NOT NULL DEFAULT 0,
      UNIQUE(provider,symbol,label)
    );
    CREATE TABLE IF NOT EXISTS shadow_events (
      track_id TEXT NOT NULL REFERENCES shadow_accounts(track_id),
      decision_date TEXT NOT NULL, session_date TEXT NOT NULL,
      action TEXT NOT NULL, target INTEGER NOT NULL,
      observed_price REAL NOT NULL, quantity REAL NOT NULL,
      simulated_cost REAL NOT NULL, cash REAL NOT NULL,
      units REAL NOT NULL, equity REAL NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY(track_id,decision_date)
    );
    CREATE TABLE IF NOT EXISTS shadow_runs (
      run_id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT NOT NULL, status TEXT NOT NULL,
      details_json TEXT NOT NULL
    );
    """)
    return con


def _account_id(asset: dict, spec: dict, settings: Settings) -> str:
    canonical = {"provider": asset["provider"], "symbol": asset["symbol"],
                 "currency": asset["currency"], "spec": spec,
                 "settings": asdict(settings)}
    return sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def bootstrap(
    db: str | Path, assets: list[dict], settings: Settings,
    *, now: datetime | None = None,
    fetch_fn: Callable = fetch,
    strategies: tuple[dict, ...] = TRACK_SPECS,
) -> dict:
    current = _utc(now)
    prepared = []
    for asset in assets:
        if (asset.get("provider") != "upbit" or
            not str(asset.get("symbol", "")).startswith("KRW-") or
            asset.get("currency") != "KRW" or asset.get("kind") != "crypto"):
            raise ValueError("Shadow onboarding supports only Upbit KRW spot, not stocks/demo")
        frame = fetch_fn(asset)
        last = _completed_utc_day(frame, current)
        validate_shadow_market(frame, current)
        for entry in strategies:
            spec = _spec(entry)
            if spec["kind"] == "momentum" and len(frame) < spec["slow"]:
                raise ValueError("Insufficient closed candles at registration")
            prepared.append((_account_id(asset, spec, settings), asset, spec, last))
    count = 0
    with _connection(db) as con:
        con.execute("BEGIN IMMEDIATE")
        for key, asset, spec, last in prepared:
            prior = con.execute(
                "SELECT track_id FROM shadow_accounts WHERE provider=? AND symbol=? AND label=?",
                (asset["provider"], asset["symbol"], spec["label"]),
            ).fetchone()
            if prior is not None:
                if prior["track_id"] != key:
                    raise ValueError("Frozen shadow strategy/config changed; no implicit overwrite")
                continue
            money = float(settings.initial_cash)
            con.execute("""INSERT INTO shadow_accounts
                (track_id,provider,symbol,currency,label,spec_json,settings_json,
                 registered_utc,frozen_bar,initial_cash,cash,units,last_equity,
                 day_date,day_start_equity,halted)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (key, asset["provider"], asset["symbol"], asset["currency"],
                 spec["label"], json.dumps(spec, sort_keys=True),
                 json.dumps(asdict(settings), sort_keys=True),
                 current.isoformat(), last, money, money, 0., money, None, money))
            count += 1
    return {"status": "REGISTERED" if count else "ALREADY_FROZEN",
            "created_tracks": count, "tracks_requested": len(prepared),
            "real_orders_enabled": False, "promotion_enabled": False,
            "note": "No earlier candles can produce simulated executions"}


def _apply(
    con: sqlite3.Connection, account: sqlite3.Row, *, decision_date: str,
    session_date: str, price: float, target: int, now: datetime,
) -> dict:
    if not math.isfinite(price) or price <= 0 or type(target) is not int or target not in (0, 1):
        raise ValueError("Invalid quote or target")
    if session_date != now.date().isoformat():
        raise ValueError("Quote session does not match UTC today")
    if (date.fromisoformat(session_date) - date.fromisoformat(decision_date)).days != 1:
        raise ValueError("Only next UTC session can receive a forward virtual fill")
    if decision_date <= account["frozen_bar"]:
        return {"status": "WAIT_NEW_BAR", "track_id": account["track_id"]}
    con.execute("BEGIN IMMEDIATE")
    old = con.execute(
        "SELECT * FROM shadow_accounts WHERE track_id=?", (account["track_id"],)
    ).fetchone()
    if old is None:
        raise ValueError("Frozen track disappeared")
    duplicate = con.execute(
        "SELECT 1 FROM shadow_events WHERE track_id=? AND decision_date=?",
        (old["track_id"], decision_date),
    ).fetchone()
    if duplicate:
        return {"status": "DUPLICATE", "track_id": old["track_id"]}
    if old["halted"]:
        return {"status": "HALTED", "track_id": old["track_id"]}
    if old["day_date"] and session_date <= old["day_date"]:
        raise ValueError("Out-of-order session date")
    cfg = Settings(**json.loads(old["settings_json"]))
    cash, qty = float(old["cash"]), float(old["units"])
    equity_pre = cash + qty * price
    start_equity = float(old["last_equity"])
    if not (math.isfinite(equity_pre) and equity_pre > 0):
        raise ValueError("Invalid virtual account equity")
    action, volume, costs = "HOLD", 0., 0.
    if equity_pre < start_equity * (1 - cfg.max_paper_daily_loss):
        action = "HALT"
        con.execute("UPDATE shadow_accounts SET halted=1 WHERE track_id=?", (old["track_id"],))
    else:
        desired_value = equity_pre * cfg.max_position_fraction if target else 0.
        delta = desired_value - qty * price
        cost_rate = (cfg.fee_bps + cfg.slippage_bps) / 10_000.
        minimum = 5000.0  # Upbit KRW min order notional approximation
        if delta >= minimum:
            spend = min(delta, cash / (1. + cost_rate))
            if spend >= minimum:
                volume = spend / price
                costs = spend * cost_rate
                cash -= spend + costs
                qty += volume
                action = "BUY"
        elif delta <= -minimum:
            volume = min(qty, abs(delta) / price)
            amount = volume * price
            if amount >= minimum:
                costs = amount * cost_rate
                cash += amount - costs
                qty -= volume
                action = "SELL"
    cash = max(0., cash)
    qty = max(0., qty)
    equity = cash + qty * price
    if not (math.isfinite(equity) and equity > 0):
        raise ValueError("Invalid post-fill equity")
    con.execute("""
        UPDATE shadow_accounts
        SET cash=?,units=?,last_equity=?,day_date=?,day_start_equity=?
        WHERE track_id=?""",
        (cash, qty, equity, session_date, start_equity, old["track_id"]),
    )
    con.execute("""
        INSERT INTO shadow_events
        (track_id,decision_date,session_date,action,target,observed_price,
         quantity,simulated_cost,cash,units,equity,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (old["track_id"], decision_date, session_date, action, target,
         price, volume, costs, cash, qty, equity, now.isoformat()),
    )
    return {"status": "RECORDED", "track_id": old["track_id"],
            "asset": old["symbol"], "strategy": old["label"],
            "action": action, "price": price, "decision_date": decision_date,
            "session_date": session_date, "virtual_equity": equity}


@contextmanager
def _exclusive_lock(db: str | Path):
    lock_path = Path(str(db) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another shadow cycle is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_cycle(
    db: str | Path, assets: list[dict], *, now: datetime | None = None,
    fetch_fn: Callable = fetch, quote_fn: Callable = live_paper_quote,
) -> dict:
    current = _utc(now)
    results, errors = [], []
    with _exclusive_lock(db), _connection(db) as con:
        frozen = con.execute("SELECT * FROM shadow_accounts ORDER BY symbol,label").fetchall()
        if not frozen:
            raise ValueError("No frozen tracks; shadow-bootstrap first")
        configured = {(a["provider"], a["symbol"]): a for a in assets}
        by_symbol: dict[tuple[str,str], list] = {}
        for row in frozen:
            by_symbol.setdefault((row["provider"], row["symbol"]), []).append(row)
        for key, tracks in by_symbol.items():
            asset = configured.get(key)
            if asset is None:
                errors.append({"asset": key[1], "error": "Frozen asset missing from config"})
                continue
            try:
                if asset.get("currency") != "KRW" or asset.get("provider") != "upbit":
                    raise ValueError("Only frozen Upbit KRW instruments allowed")
                data = fetch_fn(asset)
                day = _completed_utc_day(data, current)
                quality = validate_shadow_market(data, current)
                ready = [r for r in tracks if not r["halted"] and
                         day > r["frozen_bar"] and
                         not con.execute(
                             "SELECT 1 FROM shadow_events WHERE track_id=? AND decision_date=?",
                             (r["track_id"], day),
                         ).fetchone()]
                if not ready:
                    results.append({"asset": key[1], "status": "WAIT_NEW_BAR_OR_ALREADY_RECORDED"})
                    continue
                # Read one fresh public ticker only if a genuinely new closed bar exists.
                price, session = quote_fn(key[0], key[1], now=current)
                if session != current.date().isoformat():
                    raise ValueError("Quote does not belong to current UTC session")
                validate_public_quote(quality["last_completed_close"], price)
                for row in ready:
                    try:
                        target = _target(data, json.loads(row["spec_json"]))
                        with con:
                            outcome = _apply(
                                con, row, decision_date=day,
                                session_date=session, price=price, target=target,
                                now=current,
                            )
                        if outcome["status"] == "RECORDED":
                            last_recorded = con.execute(
                                "SELECT decision_date FROM shadow_events "
                                "WHERE track_id=? AND decision_date<? "
                                "ORDER BY decision_date DESC LIMIT 1",
                                (row["track_id"], day),
                            ).fetchone()
                            anchor = (last_recorded["decision_date"]
                                      if last_recorded else row["frozen_bar"])
                            missed = max(0, (date.fromisoformat(day) -
                                             date.fromisoformat(anchor)).days - 1)
                            outcome["unobserved_utc_days"] = missed
                        results.append(outcome)
                    except Exception as exc:
                        errors.append({"asset": key[1], "strategy": row["label"],
                                       "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
            except Exception as exc:
                errors.append({"asset": key[1], "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
        summary = {
            "status": "ERROR" if errors else "OK",
            "started_at_utc": current.isoformat(),
            "tracks": len(frozen), "results": results, "errors": errors,
            "real_orders_enabled": False, "promotion_enabled": False,
        }
        with con:
            con.execute(
                "INSERT INTO shadow_runs(created_at,status,details_json) VALUES (?,?,?)",
                (current.isoformat(), summary["status"], json.dumps(summary, sort_keys=True)),
            )
    return summary


def status(db: str | Path) -> dict:
    if not Path(db).exists():
        return {"tracks": [], "runs": [], "real_orders_enabled": False}
    with _connection(db) as con:
        accounts = con.execute("SELECT * FROM shadow_accounts ORDER BY symbol,label").fetchall()
        rows = []
        for a in accounts:
            events = con.execute(
                "SELECT decision_date,action,equity FROM shadow_events WHERE track_id=? ORDER BY decision_date",
                (a["track_id"],),
            ).fetchall()
            peak, drawdown = float(a["initial_cash"]), 0.
            for e in events:
                peak = max(peak, float(e["equity"]))
                drawdown = min(drawdown, float(e["equity"]) / peak - 1.)
            rows.append({
                "asset": a["symbol"], "strategy": a["label"], "track_id": a["track_id"],
                "frozen_bar": a["frozen_bar"], "registered_utc": a["registered_utc"],
                "events": len(events), "simulated_orders": sum(
                    e["action"] in ("BUY", "SELL") for e in events),
                "cash": a["cash"], "units": a["units"],
                "last_recorded_equity": a["last_equity"],
                "return_since_initial": float(a["last_equity"]) / float(a["initial_cash"]) - 1.,
                "max_recorded_drawdown": drawdown,
                "halted": bool(a["halted"]),
                "last_decision_date": events[-1]["decision_date"] if events else None,
                "eligible_for_promotion": False,
            })
        runs = [dict(r) for r in con.execute(
            "SELECT run_id,created_at,status FROM shadow_runs ORDER BY run_id DESC LIMIT 10"
        ).fetchall()]
    return {"tracks": rows, "runs": runs, "real_orders_enabled": False,
            "promotion_enabled": False, "caveat": "Observation-based forward virtual ledger, not live fills"}

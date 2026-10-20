"""Idempotent forward paper ledger. NEVER places real orders.

Separate accounting for each asset/currency: NOT a portfolio-level risk engine.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .settings import Settings


@dataclass(frozen=True)
class PaperFill:
    symbol: str
    decision_date: str
    session_date: str
    action: str
    price: float
    units: float
    fee: float
    cash: float
    position_units: float
    equity: float
    strategy_id: str


def connect_db(path: str | Path) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            symbol TEXT PRIMARY KEY, currency TEXT NOT NULL, cash REAL NOT NULL,
            units REAL NOT NULL DEFAULT 0, first_at TEXT NOT NULL,
            day_date TEXT, day_start_equity REAL NOT NULL,
            last_equity REAL NOT NULL, halted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS decisions (
            symbol TEXT NOT NULL, decision_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL, signal INTEGER NOT NULL,
            session_date TEXT NOT NULL, price REAL NOT NULL,
            action TEXT NOT NULL, units REAL NOT NULL,
            fee REAL NOT NULL, equity REAL NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(symbol, decision_date)
        );
    """)
    return c


def record_paper(
    path: str | Path, *, provider: str, symbol: str, currency: str,
    decision: dict, quote: float, session_date: str, settings: Settings,
    now: datetime | None = None,
) -> dict:
    """One and only one decision per *completed* bar; fail closed on stale data.

    Run only with real-time quote. If simulation is down for days, skip stale signal.
    Initial cash is PER SYMBOL, never pooled across currency/other symbols.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or provider not in {"upbit", "kis"}:
        raise ValueError("Paper only supports live Upbit/KIS quotes and aware timestamps")
    from datetime import date
    decision_day = date.fromisoformat(decision["date"])
    session_day = date.fromisoformat(session_date)
    lag_days = (session_day - decision_day).days
    if not (1 <= lag_days <= (1 if provider == "upbit" else 5)):
        raise ValueError("Not next market session or stale last completed bar")
    if not 0 < quote < float("inf") or decision["target"] not in (0, 1):
        raise ValueError("Invalid quote/target")
    if not decision.get("strategy_id") or not symbol or not currency:
        raise ValueError("Missing strategy/symbol/currency")
    ts = current.astimezone(timezone.utc).isoformat()
    with connect_db(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT action, units, price, equity FROM decisions WHERE symbol=? AND decision_date=?",
                                (symbol, decision["date"])).fetchone()
        if previous:
            return {"status": "DUPLICATE_SKIPPED", "symbol": symbol, "decision_date": decision["date"]}
        account = conn.execute("SELECT currency,cash,units,day_date,day_start_equity,last_equity,halted FROM accounts WHERE symbol=?", (symbol,)).fetchone()
        if not account:
            conn.execute("INSERT INTO accounts VALUES (?, ?, ?, 0, ?, ?, ?, ?, 0)",
                         (symbol, currency, settings.initial_cash, ts, session_date,
                          settings.initial_cash, settings.initial_cash))
            account = (currency, settings.initial_cash, 0.0, session_date,
                       settings.initial_cash, settings.initial_cash, 0)
        acurrency, cash, units, day_date, day_start, last_equity, halted = account
        if acurrency != currency:
            raise ValueError("Paper account currency mismatch")
        if halted:
            raise ValueError("Paper account halted; manually inspect before resuming")
        equity = cash + units * quote
        if day_date != session_date:
            day_start = last_equity  # previous observation, not today's fallen quote
        if equity < day_start * (1 - settings.max_paper_daily_loss):
            conn.execute("UPDATE accounts SET halted=1 WHERE symbol=?", (symbol,))
            return {"status": "HALTED", "reason": "Daily paper loss limit breached; inspect before resuming"}
        # target long = <= 20% of this symbol's standalone paper equity
        desired_value = settings.max_position_fraction * equity if decision["target"] else 0.0
        existing_value = units * quote
        change = desired_value - existing_value
        fee_rate = (settings.fee_bps + settings.slippage_bps) / 10000
        min_value = 5000 if provider == "upbit" else quote  # 1 share minimum for KIS
        action, size, fees = "HOLD", 0.0, 0.0
        if change > min_value:
            spend = min(change, cash / (1 + fee_rate))
            size = spend / quote
            if provider == "kis":
                size = float(int(size))
            if size * quote >= min_value:
                fees = size * quote * fee_rate
                cash -= size * quote + fees
                units += size
                action = "BUY"
        elif change < -min_value:
            size = min(units, abs(change) / quote)
            if provider == "kis":
                size = float(int(size))
            if size * quote >= min_value:
                fees = size * quote * fee_rate
                cash += size * quote - fees
                units -= size
                action = "SELL"
        cash, units = max(0.0, cash), max(0.0, units)
        equity = cash + units * quote
        conn.execute("UPDATE accounts SET cash=?, units=?, day_date=?, day_start_equity=?, last_equity=? WHERE symbol=?",
                     (cash, units, session_date, day_start, equity, symbol))
        conn.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (symbol, decision["date"], decision["strategy_id"], int(decision["target"]),
                      session_date, quote, action, size, fees, equity, ts))
        result = asdict(PaperFill(symbol, decision["date"], session_date, action, quote,
                                  size, fees, cash, units, equity, decision["strategy_id"]))
        return {"status": "PAPER_RECORDED", "fill": result}


def paper_statistics(path: str | Path, symbol: str, strategy_id: str) -> dict:
    with connect_db(path) as conn:
        rows = conn.execute("SELECT session_date, action, equity FROM decisions WHERE symbol=? AND strategy_id=? ORDER BY session_date",
                            (symbol, strategy_id)).fetchall()
    if not rows:
        return {"days": 0, "orders": 0, "paper_return": None, "paper_max_drawdown": None}
    from datetime import date
    elapsed = (date.fromisoformat(rows[-1][0]) - date.fromisoformat(rows[0][0])).days + 1
    equity = [r[2] for r in rows]
    peak, max_dd = equity[0], 0.0
    for x in equity:
        peak = max(peak, x)
        max_dd = min(max_dd, x / peak - 1.0)
    return {"days": elapsed, "orders": sum(r[1] in ("BUY", "SELL") for r in rows),
            "paper_return": equity[-1] / equity[0] - 1,
            "paper_max_drawdown": max_dd}


def promotion_gate(research_result: dict, paper: dict, settings: Settings, *, manual_authorization: bool = False) -> dict:
    """Read-only eligibility assessment; trading live is NOT implemented in v0.1."""
    eligible = bool(research_result.get("status") == "PAPER_ELIGIBLE"
                    and paper["days"] >= settings.paper_min_days
                    and paper["orders"] >= settings.paper_min_orders
                    and paper["paper_return"] is not None and paper["paper_return"] > 0
                    and paper["paper_max_drawdown"] is not None
                    and abs(paper["paper_max_drawdown"]) <= settings.max_drawdown)
    return {"live_candidate": eligible, "manual_authorization": manual_authorization,
            "real_orders_enabled": False,
            "reason": "Research and forward paper both passed; live broker execution intentionally not included" if eligible else "Forward paper incubation or research gates have not passed"}

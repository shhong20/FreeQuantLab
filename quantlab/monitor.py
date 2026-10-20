"""Read-only Shadow Paper telemetry and anomaly detection.

No broker endpoints, no mutation, no strategy activation. SQLite opened mode=ro
and PRAGMA query_only=ON; dashboard never starts the paper runner.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
SCHEDULE_END_KST = (10, 20)  # Last retry at 10:00, plus grace period.


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Run timestamps must be timezone aware")
    return parsed.astimezone(timezone.utc)


def _read_db(path: str | Path) -> sqlite3.Connection:
    db = Path(path).resolve()
    if not db.is_file():
        raise FileNotFoundError("Shadow Paper database is not initialized")
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=3000")
    return con


def _alert(severity: str, code: str, message: str, **context) -> dict:
    return {"severity": severity, "code": code, "message": message, **context}


def snapshot(path: str | Path, *, now: datetime | None = None) -> dict:
    """Consistent read-only snapshot; no fabricated marks for missing quote days.

    The only known equity points are actually recorded paper observations.
    A track without an event has ZERO observed PnL, not a backtested return.
    """
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Timezone-aware now required")
    instant = instant.astimezone(timezone.utc)
    local = instant.astimezone(KST)
    expected_decision = (instant.date() - timedelta(days=1)).isoformat()
    after_deadline = (local.hour, local.minute) >= SCHEDULE_END_KST
    alerts: list[dict] = []
    with _read_db(path) as con:
        # BEGIN gives us one consistent WAL read snapshot across three tables.
        con.execute("BEGIN")
        accounts = con.execute(
            "SELECT * FROM shadow_accounts ORDER BY symbol,label"
        ).fetchall()
        events = con.execute(
            "SELECT e.*,a.symbol,a.label FROM shadow_events e "
            "JOIN shadow_accounts a USING (track_id) "
            "ORDER BY e.session_date DESC, e.created_at DESC"
        ).fetchall()
        runs = con.execute(
            "SELECT run_id,created_at,status,details_json FROM shadow_runs "
            "ORDER BY run_id DESC LIMIT 100"
        ).fetchall()
        con.execute("COMMIT")

    history: dict[str,list[dict]] = {}
    for e in events:
        history.setdefault(e["track_id"], []).append({
            "date": e["session_date"],
            "decision_date": e["decision_date"],
            "equity": float(e["equity"]),
            "action": e["action"],
            "price": float(e["observed_price"]),
            "quantity": float(e["quantity"]),
            "cost": float(e["simulated_cost"]),
            "symbol": e["symbol"],
            "strategy": e["label"],
        })
    for items in history.values():
        items.sort(key=lambda e: e["date"])

    tracks = []
    for a in accounts:
        key = a["track_id"]
        initial = float(a["initial_cash"])
        recorded = history.get(key, [])
        equity_series = [{"date": a["registered_utc"][:10], "equity": initial}]
        equity_series += [{"date": e["date"], "equity": e["equity"]} for e in recorded]
        full_return = float(a["last_equity"]) / initial - 1.0
        peak, drawdown = initial, 0.0
        for event in recorded:
            peak = max(peak, event["equity"])
            drawdown = min(drawdown, event["equity"] / peak - 1.0)
        last_day = recorded[-1]["decision_date"] if recorded else None
        eligible = a["frozen_bar"] < expected_decision
        if a["halted"]:
            state = "halted"
            alerts.append(_alert(
                "critical", "RISK_HALTED",
                "Virtual risk halt is active. No automatic reset is permitted.",
                symbol=a["symbol"], strategy=a["label"],
            ))
        elif eligible and last_day != expected_decision and after_deadline:
            state = "overdue"
            alerts.append(_alert(
                "warning", "DECISION_MISSING",
                "A newly completed UTC bar has not produced a paper event by 10:20 KST.",
                symbol=a["symbol"], strategy=a["label"],
                expected_decision_date=expected_decision,
            ))
        elif eligible and last_day != expected_decision:
            state = "awaiting_cycle"
        elif not eligible:
            state = "incubating"
        else:
            state = "recorded"
        tracks.append({
            "id": key, "symbol": a["symbol"], "strategy": a["label"],
            "state": state, "registered_utc": a["registered_utc"],
            "frozen_bar": a["frozen_bar"], "last_decision_date": last_day,
            "events": len(recorded), "simulated_orders": sum(
                e["action"] in ("BUY","SELL") for e in recorded),
            "cash": float(a["cash"]), "units": float(a["units"]),
            "initial_virtual_cash": initial,
            "recorded_equity": float(a["last_equity"]),
            "observed_return": full_return if recorded else None,
            "max_recorded_drawdown": drawdown if recorded else None,
            "halted": bool(a["halted"]),
            "equity_points": equity_series,
            "promotion_eligible": False,
        })

    if not accounts:
        alerts.append(_alert("critical","NO_TRACKS","No registered virtual tracking accounts."))
    last_run = None
    failures_last_week = 0
    run_records: list[dict] = []
    for i, r in enumerate(runs):
        when = _time(r["created_at"])
        details = json.loads(r["details_json"])
        if not isinstance(details, dict):
            raise ValueError("Malformed run details")
        errors = details.get("errors", [])
        # Send bounded messages only. No secrets/keys should exist in the database.
        if not isinstance(errors, list):
            errors = []
        failure = {
            "id": r["run_id"], "created_at": when.isoformat(),
            "status": r["status"], "errors": [
                {"asset": str(e.get("asset",""))[:40],
                 "strategy": str(e.get("strategy",""))[:70],
                 "error": str(e.get("error",""))[:180]}
                for e in errors[:8] if isinstance(e, dict)
            ],
        }
        run_records.append(failure)
        if i == 0:
            last_run = failure
        if r["status"] == "ERROR" and instant - when <= timedelta(days=7):
            failures_last_week += 1
    if last_run and last_run["status"] != "OK":
        alerts.append(_alert("warning","LATEST_RUN_FAILED",
                             "The latest paper cycle returned an error. Check service journal."))
    if after_deadline and any(a["frozen_bar"] < expected_decision for a in accounts):
        if not last_run or _time(last_run["created_at"]).date() < instant.date():
            alerts.append(_alert(
                "warning", "NO_CYCLE_TODAY",
                "No paper-cycle execution has been recorded today (UTC).",
            ))

    critical_count = sum(a["severity"] == "critical" for a in alerts)
    warning_count = sum(a["severity"] == "warning" for a in alerts)
    return {
        "generated_utc": instant.isoformat(),
        "generated_kst": local.isoformat(),
        "market": "Upbit KRW spot / observation-based forward virtual tracking",
        "real_orders_enabled": False, "automatic_promotion_enabled": False,
        "summary": {
            "tracks": len(tracks),
            "assets": len({t["symbol"] for t in tracks}),
            "virtual_orders": sum(t["simulated_orders"] for t in tracks),
            "recorded_events": sum(t["events"] for t in tracks),
            "halted": sum(t["halted"] for t in tracks),
            "critical_alerts": critical_count, "warnings": warning_count,
            "last_run": last_run["created_at"] if last_run else None,
            "last_run_status": last_run["status"] if last_run else "NEVER",
            "failures_last_7d_in_latest_100_runs": failures_last_week,
            "expected_closed_candle_utc": expected_decision,
        },
        "alerts": alerts,
        "tracks": tracks,
        "recent_events": [dict(x) for x in sorted(
            (event for stream in history.values() for event in stream),
            key=lambda x: (x["date"],x["symbol"],x["strategy"]), reverse=True
        )[:80]],
        "recent_runs": run_records[:25],
        "caveats": [
            "No actual exchange orders or balances. Virtual valuation is observed only at recorded quotes.",
            "No returns are shown before the first forward observation; initial cash is not a historical backtest.",
            "The bootstrap/strategy research engine is NOT automatically connected to paper or live accounts.",
        ],
    }

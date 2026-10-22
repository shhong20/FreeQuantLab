"""Read-only warning about held positions after permanent virtual risk HALT.

It deliberately does NOT adjust portfolio equity, fabricate delayed executions,
alter preregistered strategies, or infer the current executable liquidation price.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from .monitor import _read_db


def inspect_halted_positions(db: str | Path, *, now: datetime | None = None) -> dict:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("Timezone-aware audit time required")
    with closing(_read_db(db)) as conn:
        conn.execute("BEGIN")
        try:
            accounts = conn.execute(
                "SELECT track_id,symbol,label,halted,units,cash,last_equity,day_date "
                "FROM shadow_accounts ORDER BY symbol,label"
            ).fetchall()
            events = conn.execute(
                "SELECT track_id,action,session_date,created_at,units,equity "
                "FROM shadow_events ORDER BY session_date,created_at"
            ).fetchall()
        finally:
            conn.execute("ROLLBACK")
    latest = {e["track_id"]: e for e in events}
    halted = []
    for a in accounts:
        if not a["halted"]:
            continue
        latest_event = latest.get(a["track_id"])
        if latest_event is None or latest_event["action"] != "HALT":
            raise ValueError("HALT account is not backed by HALT event")
        held = float(a["units"])
        if not math.isfinite(held) or held < 0:
            raise ValueError("Negative units in frozen ledger")
        halted.append({
            "track_id":a["track_id"],"symbol":a["symbol"],
            "strategy":a["label"],
            "units_remaining_at_last_record":held,
            "recorded_cash":float(a["cash"]),
            "last_recorded_equity":float(a["last_equity"]),
            "last_event_session":latest_event["session_date"],
            "last_event_action":"HALT",
            "inventory_remains_unliquidated":held > 0,
            "current_liquidation_price_verified":False,
            "current_position_equity_verified":False,
            "last_recorded_equity_is_not_current_equity":True,
        })
    held = [a for a in halted if a["inventory_remains_unliquidated"]]
    return {
        "status":("HALTED_OPEN_EXPOSURE_UNMARKED" if held else
                  "HALTED_NO_OPEN_EXPOSURE" if halted else "NO_HALTED_ACCOUNTS"),
        "checked_at_utc":stamp.astimezone(timezone.utc).isoformat(),
        "total_accounts":len(accounts),"halted_accounts":len(halted),
        "halted_with_open_inventory":len(held),
        "accounts":halted,
        "has_not_marked_to_market_after_halt":bool(held),
        "not_a_current_pnl_or_broker_fill_report":True,
        "frozen_90_day_study_modified":False,
        "repair_performed":False,
        "real_orders_enabled":False,
        "automatic_strategy_promotion":False,
    }


def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser(description="Read-only risk-HALT residual exposure audit")
    ap.add_argument("--db",default="/home/hong/apps/FreeQuantLab/state/shadow.db")
    args=ap.parse_args(argv)
    try:
        print(json.dumps(inspect_halted_positions(args.db),ensure_ascii=False,sort_keys=True))
        return 0
    except (ValueError,RuntimeError,KeyError,TypeError,OSError,sqlite3.Error) as exc:
        print(json.dumps({"status":"HALT_EXPOSURE_AUDIT_BLOCKED",
                          "reason":type(exc).__name__}),file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

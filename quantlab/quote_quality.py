"""Forward paper public-ticker evidence and *proxy* execution-quality diagnostics.

A public Upbit last-traded ticker is NOT a tradable ask/bid, independent
execution venue quote, broker acknowledgement or real fill. Costs are modelled
assumptions, not actual realized slippage. Never infer real liquidity/alpha.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import sqlite3

from .monitor import _read_db
from .settings import Settings
import json


def execution_proxy_quality(shadow_db: str | Path) -> dict:
    issues: list[dict] = []
    with _read_db(shadow_db) as con:
        con.execute("BEGIN")
        accounts = con.execute(
            "SELECT track_id,symbol,label,settings_json FROM shadow_accounts"
        ).fetchall()
        records = con.execute(
            "SELECT e.track_id,e.decision_date,e.session_date,e.action,"
            "e.observed_price,e.quantity,e.simulated_cost,e.created_at,"
            "a.symbol,a.label,a.settings_json FROM shadow_events e "
            "JOIN shadow_accounts a ON e.track_id=a.track_id "
            "ORDER BY e.decision_date,e.track_id"
        ).fetchall()
        has_evidence = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_quote_evidence'"
        ).fetchone() is not None
        quotes = con.execute(
            "SELECT * FROM shadow_quote_evidence ORDER BY symbol,decision_date"
        ).fetchall() if has_evidence else []
        con.execute("COMMIT")
    by_quote = {(row["symbol"],row["decision_date"]):row for row in quotes}
    covered=0
    missing=0
    gaps=[]
    delay_minutes=[]
    cost_total=0.
    traded_notional=0.
    virtual_orders=0
    details=[]
    used_evidence=set()
    for r in records:
        key=(r["symbol"],r["decision_date"])
        quote=by_quote.get(key)
        cost=float(r["simulated_cost"])
        qty=float(r["quantity"])
        px=float(r["observed_price"])
        cfg=Settings(**json.loads(r["settings_json"]))
        if r["action"] in ("BUY","SELL"):
            virtual_orders+=1
            notional=qty*px
            traded_notional+=notional
            cost_total+=cost
            theoretical=notional*(cfg.fee_bps+cfg.slippage_bps)/10000
            if not math.isclose(cost,theoretical,rel_tol=1e-7,abs_tol=.0001):
                issues.append({"code":"COST_ASSUMPTION_MISMATCH",
                               "symbol":r["symbol"],"decision_date":r["decision_date"]})
        if quote is None:
            missing+=1
            continue
        covered+=1
        used_evidence.add(key)
        if r["session_date"]!=quote["session_date"] or not math.isclose(
            px,float(quote["public_ticker_price"]),rel_tol=1e-10,abs_tol=1e-7
        ):
            issues.append({"code":"QUOTE_EVENT_PRICE_MISMATCH",
                           "symbol":r["symbol"],"decision_date":r["decision_date"]})
        timestamp=datetime.fromisoformat(quote["observed_at_utc"])
        recorded=datetime.fromisoformat(r["created_at"])
        if timestamp.tzinfo is None or recorded.tzinfo is None:
            issues.append({"code":"NAIVE_QUOTE_TIMESTAMP",
                           "symbol":r["symbol"],"decision_date":r["decision_date"]})
            continue
        stamp=timestamp.astimezone(timezone.utc)
        lag=(stamp-datetime.fromisoformat(
            quote["session_date"]+"T00:00:00+00:00"
        )).total_seconds()/60
        if (lag<0 or recorded.astimezone(timezone.utc)<stamp-timedelta(seconds=2)
                or recorded.astimezone(timezone.utc)>stamp+timedelta(seconds=120)):
            issues.append({"code":"QUOTE_TIME_INVALID_OR_UNMATCHED",
                           "symbol":r["symbol"],"decision_date":r["decision_date"]})
        if lag>150:
            issues.append({"code":"DELAYED_PUBLIC_TICKER_OBSERVATION",
                           "symbol":r["symbol"],"decision_date":r["decision_date"]})
        reference=float(quote["prior_completed_close"])
        if not math.isfinite(reference) or reference<=0 or not math.isfinite(float(quote["public_ticker_price"])):
            issues.append({"code":"INVALID_SAVED_QUOTE_EVIDENCE",
                           "symbol":r["symbol"],"decision_date":r["decision_date"]})
            continue
        gap=10000*(px/reference-1.)
        gaps.append(gap)
        delay_minutes.append(lag)
        details.append({
            "symbol":r["symbol"],"strategy":r["label"],
            "decision_date":r["decision_date"],"action":r["action"],
            "public_ticker_vs_prev_close_bps":round(gap,3),
            "minutes_after_utc_daily_close":round(lag,3),
            "virtual_order":r["action"] in ("BUY","SELL"),
            "simulated_cost_krw":round(cost,5),
        })
    for key in by_quote.keys()-used_evidence:
        issues.append({"code":"QUOTE_EVIDENCE_HAS_NO_EVENT",
                       "symbol":key[0],"decision_date":key[1]})
    return {
        "status":"FAIL" if any(i["code"] in {
            "COST_ASSUMPTION_MISMATCH","QUOTE_EVENT_PRICE_MISMATCH",
            "NAIVE_QUOTE_TIMESTAMP","QUOTE_TIME_INVALID_OR_UNMATCHED",
            "INVALID_SAVED_QUOTE_EVIDENCE",
        } for i in issues) else "REVIEW" if issues else "AWAITING_FIRST_OBSERVATION" if not records else "PASS",
        "source":"upbit_public_last_trade_not_executable_order_book",
        "quote_rows":len(quotes),
        "virtual_event_rows":len(records),
        "events_with_quote_evidence":covered,
        "legacy_events_without_quote_evidence":missing,
        "virtual_buy_sell_orders":virtual_orders,
        "model_assumed_total_cost_krw":round(cost_total,5),
        "virtual_order_notional_krw":round(traded_notional,5),
        "assumed_cost_rate_bps":(round(cost_total/traded_notional*10000,4)
                                 if traded_notional else None),
        "max_abs_public_ticker_prev_close_gap_bps":(
            round(max(map(abs,gaps)),3) if gaps else None),
        "max_public_ticker_minutes_after_close":(
            round(max(delay_minutes),3) if delay_minutes else None),
        "issues":issues[:100],"recent_observations":details[-80:],
        "not_actual_fill_quality":True,
        "no_executable_spread_or_order_book_evidence":True,
        "real_orders_enabled":False,
        "warning":"Quote-to-prior-close gap is a market movement proxy, NOT executed slippage. "
                  "Fees and slippage are assumed model costs, not live fills.",
    }

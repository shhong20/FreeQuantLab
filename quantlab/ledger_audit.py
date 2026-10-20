"""Forensic, read-only integrity checks of the immutable Shadow Paper ledger.

Reconstruct each account's cash/units from recorded events and verify cash,
quantity, fees, equity, dates, frozen strategy identity and last account state.
Never repair, edit, reset stops, or infer missing market prices.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3

from .monitor import _read_db
from .settings import Settings


def _close(a: float, b: float, *, abs_tol: float = .0005) -> bool:
    return math.isfinite(float(a)) and math.isfinite(float(b)) and math.isclose(
        float(a),float(b),rel_tol=1e-9,abs_tol=abs_tol
    )


def audit_shadow_ledger(path: str | Path, *, now: datetime | None = None) -> dict:
    """Return fail-closed evidence; issues are visible to watchdog, not auto-fixed."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Timezone-aware now required")
    issues, tracks = [], []
    with _read_db(path) as con:
        con.execute("BEGIN")
        integ = con.execute("PRAGMA quick_check").fetchone()
        accounts = con.execute("SELECT * FROM shadow_accounts ORDER BY symbol,label").fetchall()
        events = con.execute(
            "SELECT * FROM shadow_events ORDER BY track_id,decision_date"
        ).fetchall()
        con.execute("COMMIT")
    if integ is None or integ[0] != "ok":
        issues.append({"code":"SQLITE_INTEGRITY_FAILED","track_id":None,
                       "reason":"SQLite quick_check failed"})
    by_id: dict[str,list] = {}
    for event in events:
        by_id.setdefault(event["track_id"],[]).append(event)
    registered_ids = {a["track_id"] for a in accounts}
    for unknown in sorted(set(by_id)-registered_ids):
        issues.append({"code":"ORPHAN_EVENT","track_id":unknown,"reason":"Missing parent account"})

    for a in accounts:
        key=a["track_id"]
        errors=[]
        try:
            spec=json.loads(a["spec_json"])
            setting_obj=json.loads(a["settings_json"])
            cfg=Settings(**setting_obj)
            identity={
                "provider":a["provider"],"symbol":a["symbol"],
                "currency":a["currency"],"spec":spec,
                "settings":asdict(cfg),
            }
            computed=sha256(json.dumps(identity,sort_keys=True,separators=(",",":")).encode()).hexdigest()[:24]
            if computed!=key:
                errors.append("FROZEN_STRATEGY_IDENTITY_CHANGED")
            from .shadow import _spec
            _spec(spec)
            if a["provider"]!="upbit" or a["currency"]!="KRW":
                errors.append("INVALID_ASSET_CLASS")
            initial=float(a["initial_cash"])
            if not math.isfinite(initial) or initial<=0 or not _close(initial,cfg.initial_cash):
                errors.append("INVALID_INITIAL_CAPITAL")
            frozen=date.fromisoformat(a["frozen_bar"])
            registered=datetime.fromisoformat(a["registered_utc"])
            if registered.tzinfo is None:
                errors.append("NAIVE_REGISTRATION_TIMESTAMP")
            cash, units=initial,0.
            last_event=None
            for e in by_id.get(key,[]):
                day=date.fromisoformat(e["decision_date"])
                session=date.fromisoformat(e["session_date"])
                recorded=datetime.fromisoformat(e["created_at"])
                if (day<=frozen or session-day!=timedelta(days=1) or
                    (last_event is not None and day<=last_event)):
                    errors.append("NON_FORWARD_OR_OUT_OF_ORDER_EVENT")
                if recorded.tzinfo is None or recorded.astimezone(timezone.utc).date()!=session:
                    errors.append("EVENT_TIME_SESSION_MISMATCH")
                if not (e["action"] in ("BUY","SELL","HOLD","HALT") and e["target"] in (0,1)):
                    errors.append("INVALID_EVENT_ACTION")
                price,qty,cost,post_cash,post_units,equity=map(float,(
                    e["observed_price"],e["quantity"],e["simulated_cost"],
                    e["cash"],e["units"],e["equity"]
                ))
                if any(not math.isfinite(x) for x in (price,qty,cost,post_cash,post_units,equity)):
                    errors.append("NONFINITE_ACCOUNT_EVENT")
                    break
                if price<=0 or qty<0 or cost<0 or post_cash<-.001 or post_units<-.00000001 or equity<=0:
                    errors.append("NEGATIVE_OR_INVALID_BALANCE")
                rate=(cfg.fee_bps+cfg.slippage_bps)/10000.
                if e["action"]=="BUY":
                    expected_cost=price*qty*rate
                    cash-=price*qty+cost
                    units+=qty
                    if qty<=0 or not _close(cost,expected_cost):
                        errors.append("INCORRECT_BUY_FEE_OR_VOLUME")
                elif e["action"]=="SELL":
                    expected_cost=price*qty*rate
                    cash+=price*qty-cost
                    units-=qty
                    if qty<=0 or not _close(cost,expected_cost):
                        errors.append("INCORRECT_SELL_FEE_OR_VOLUME")
                elif qty!=0 or cost!=0:
                    errors.append("NONZERO_TRADE_ON_HOLD_OR_HALT")
                if not _close(cash,post_cash) or not _close(units,post_units,abs_tol=1e-10):
                    errors.append("REPLAY_BALANCE_MISMATCH")
                if not _close(post_cash+post_units*price,equity):
                    errors.append("MARK_TO_MARKET_EQUITY_MISMATCH")
                last_event=day
            if not _close(cash,float(a["cash"])) or not _close(units,float(a["units"]),abs_tol=1e-10):
                errors.append("ACCOUNT_STATE_DIFFERS_FROM_REPLAY")
            latest=by_id.get(key,[])
            last=latest[-1] if latest else None
            if last:
                if not _close(float(a["last_equity"]),float(last["equity"])):
                    errors.append("LAST_EQUITY_DIFFERS_FROM_EVENT")
                if a["day_date"]!=last["session_date"]:
                    errors.append("LAST_SESSION_DIFFERS_FROM_EVENT")
            elif (not _close(float(a["last_equity"]),initial) or
                  a["day_date"] is not None or bool(a["halted"])):
                errors.append("UNRECORDED_ACCOUNT_MUTATION")
            if bool(a["halted"]) and (not last or last["action"]!="HALT"):
                errors.append("HALTED_WITHOUT_FINAL_HALT_EVENT")
        except (KeyError,ValueError,TypeError,OverflowError,sqlite3.Error) as exc:
            errors.append("INVALID_LEDGER_SCHEMA_OR_VALUE")
        for code in dict.fromkeys(errors):
            issues.append({"code":code,"track_id":key,
                           "reason":"Ledger value/identity mismatch; manual review required"})
        tracks.append({"track_id":key,"symbol":a["symbol"],"strategy":a["label"],
                       "events":len(by_id.get(key,[])),"status":"FAIL" if errors else "PASS"})
    if not tracks:
        issues.append({"code":"NO_FROZEN_TRACKS","track_id":None,
                       "reason":"No accounts registered"})
    return {
        "status":"FAIL" if issues else "PASS",
        "checked_at_utc":instant.astimezone(timezone.utc).isoformat(),
        "accounts":len(tracks),"events":len(events),
        "issues":issues,"tracks":tracks,
        "real_orders_enabled":False,"repair_performed":False,
    }


def evaluate_manual_review(
    report: dict, audit: dict, *,
    minimum_observation_days: int = 90,
    minimum_virtual_orders: int = 8,
) -> dict:
    """Conservative RESEARCH MATURITY label, NEVER investment approval.

    This is *not* a statistical proof, nor a substitute for later genuinely
    untouched data, real fill modeling, tax/spread checks or human approval.
    """
    if minimum_observation_days<30 or minimum_virtual_orders<1:
        raise ValueError("Invalid maturity requirements")
    audit_map={r["track_id"]:r["status"] for r in audit["tracks"]}
    results=[]
    for item in report["strategies"]:
        checks={
            "ledger_reconciles":audit["status"]=="PASS" and audit_map.get(item["track_id"])=="PASS",
            "at_least_90_or_configured_forward_days":item["recorded_days"]>=minimum_observation_days,
            "no_missing_forward_observation_days":item["missing_intermediate_utc_days"]==0,
            "minimum_executed_virtual_orders":item["simulated_orders"]>=minimum_virtual_orders,
            "not_risk_halted":not item["halted"],
            "maximum_observed_drawdown_under_12pct":item["observed_max_drawdown"] is not None and item["observed_max_drawdown"]>=-.12,
            "same_date_hold_benchmark":item["comparison_observations_aligned"],
            "positive_excess_vs_hold":item["vs_buy_hold_return_pp"] is not None and item["vs_buy_hold_return_pp"]>0,
        }
        results.append({
            "symbol":item["symbol"],"strategy":item["strategy"],
            "track_id":item["track_id"],"checks":checks,
            "research_maturity":"READY_FOR_HUMAN_REVIEW" if all(checks.values()) else "NOT_READY",
            "missing_checks":[key for key,ok in checks.items() if not ok],
            "automatic_promotion":False,"live_order_permission":False,
        })
    return {
        "criteria_kind":"research_maturity_only_not_proven_alpha",
        "minimum_forward_observation_days":minimum_observation_days,
        "minimum_virtual_orders":minimum_virtual_orders,
        "strategy_auto_promotion":False,
        "any_broker_order_permission":False,
        "strategies":results,
        "limitations":[
            "Multiple strategy comparisons and prior backtest selection remain uncorrected.",
            "The observed forward record must not be reused as a fresh independent test.",
            "Missing a UTC day or few virtual trades is a quality failure for review.",
            "Passing these checks only permits an independent human investigation, not live orders.",
        ],
    }

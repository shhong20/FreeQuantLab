"""Three-lane future-day coverage and strictly gated descriptive comparison.

Never mixes research ledgers, changes virtual accounts, or reveals protocol
return scores before preregistered original 90-day cutoff. Enforces identical
decision days / modeled risk & costs / ticker observation comparability.
"""
from __future__ import annotations

from datetime import datetime,timedelta,date,timezone
import math
from pathlib import Path

from .monitor import _read_db
from .prospective import _utc,read_protocol
from .protocol_compliance import assess_collection,_due_decision
from .ledger_audit import audit_shadow_ledger
from .candidate_lab import read_lab_plan,_verify_registered
from .qlib_lab import verify_plan,_verify_accounts
from .qlib_decision_audit import audit_decisions


LANES=("registered_90d","exaone_manual","qlib_manual")


def compare_three_lanes(
    production_db: str | Path, agent_db: str | Path, qlib_db: str | Path,
    protocol_file: str | Path, agent_plan: str | Path, qlib_plan: str | Path,
    *, now: datetime | None = None, min_complete_days: int = 90,
    price_tolerance_bps: float = 20.,
) -> dict:
    current=_utc(now)
    protocol=read_protocol(protocol_file)
    start=date.fromisoformat(protocol["first_eligible_decision_utc"])
    end=date.fromisoformat(protocol["last_eligible_decision_utc"])
    opening=datetime.fromisoformat(protocol["evaluation_not_before_utc"])
    n=(end-start).days+1
    if min_complete_days!=n or n!=90:
        raise ValueError("Existing predeclared 90-day horizon cannot change")
    if not 0<=price_tolerance_bps<=100:
        raise ValueError("Price parity cap must be 0..100 bps")
    paths=(production_db,agent_db,qlib_db)
    if len({Path(p).resolve() for p in paths})!=3:
        raise ValueError("Research lanes must use distinct databases")
    if [Path(p).name for p in paths]!=[
        "shadow.db","agent_lab_shadow.db","qlib_lab_shadow.db",
    ]:
        raise ValueError("Unexpected protected lane DB basename")
    agents=read_lab_plan(agent_plan)
    qplan=verify_plan(qlib_plan)
    if {e["asset"]["symbol"] for e in agents["entries"]}!={"KRW-BTC","KRW-ETH"}:
        raise ValueError("Agent lane is missing a crypto symbol")
    if {e["asset"]["symbol"] for e in qplan["entries"]}!={"KRW-BTC","KRW-ETH"}:
        raise ValueError("Qlib lane is missing a crypto symbol")
    valid_ledger={}
    accounts_by_lane={}
    events_by_lane={}
    quote_by_lane={}
    for lane,db in zip(LANES,paths):
        audit=audit_shadow_ledger(db,now=current)
        valid_ledger[lane]=audit["status"]=="PASS"
        with _read_db(db) as con:
            con.execute("BEGIN")
            accounts=con.execute("SELECT * FROM shadow_accounts").fetchall()
            events=con.execute("SELECT * FROM shadow_events ORDER BY track_id,decision_date").fetchall()
            quotes=con.execute("SELECT * FROM shadow_quote_evidence").fetchall()
            con.execute("COMMIT")
        accounts_by_lane[lane]=accounts
        streams={a["track_id"]:[] for a in accounts}
        for e in events:
            streams.setdefault(e["track_id"],[]).append(e)
        events_by_lane[lane]=streams
        quote_by_lane[lane]={(q["symbol"],q["decision_date"]):q for q in quotes}
    issues=[]
    if not all(valid_ledger.values()):
        issues.append("INVALID_OR_UNRECONCILED_LEDGER")
    main=assess_collection(production_db,protocol_file,now=current)
    if main["status"] in ("INVALID_PROTOCOL_IDENTITY","INVALID_LEDGER","UNAVAILABLE"):
        issues.append("FROZEN_90D_IDENTITY_FAILED")
    # A same-day comparison cannot treat already-open baseline inventory as
    # if every track began flat at the registered evaluation start.
    # The legacy 2026-10-20 BUYs are preserved but disqualify this cohort
    # from a clean 90-day cross-lane claim until manually reviewed.
    if any(item.get("code")=="PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY"
           for item in main.get("issues",[])):
        issues.append("FROZEN_90D_PRE_WINDOW_EXPOSURE_REVIEW_REQUIRED")
    try:
        _verify_registered(agent_db,agents)
        _verify_accounts(qlib_db,qplan)
    except (ValueError,KeyError,TypeError):
        issues.append("ISOLATED_LANE_IDENTITY_CHANGED")
    qlib_audit=audit_decisions(qlib_db,qplan)
    if qlib_audit["status"]=="FAIL":
        issues.append("QLIB_DECISION_REPLAY_FAILED")
    # Each independent virtual account must start with identical assumptions.
    config=None
    all_accounts=[]
    for lane,accounts in accounts_by_lane.items():
        for account in accounts:
            import json
            settings=json.loads(account["settings_json"])
            effective={k:settings[k] for k in (
                "fee_bps","slippage_bps","initial_cash",
                "max_position_fraction","max_paper_daily_loss"
            )}
            if config is None:
                config=effective
            elif config!=effective:
                issues.append("INCONSISTENT_COST_CAPITAL_OR_RISK_SETTINGS")
            all_accounts.append((lane,account))
    if len(all_accounts)!=10:
        issues.append("EXPECTED_EXACTLY_TEN_FROZEN_ACCOUNTS")
    count_by_sym={}
    for lane,a in all_accounts:
        count_by_sym.setdefault(a["symbol"],[]).append((lane,a))
    if any(len(count_by_sym.get(sym,[]))!=5 for sym in ("KRW-BTC","KRW-ETH")):
        issues.append("EXPECTED_FIVE_VIRTUAL_STRATEGIES_PER_SYMBOL")
    latest_due=_due_decision(current,start,end)
    due=0 if latest_due is None else (latest_due-start).days+1
    required=[(start+timedelta(days=i)).isoformat() for i in range(n)]
    due_set=set(required[:due])
    coverage=[]
    ready=True
    for lane,a in all_accounts:
        rows=events_by_lane[lane].get(a["track_id"],[])
        window={e["decision_date"]:e for e in rows
                if start.isoformat()<=e["decision_date"]<=end.isoformat()}
        got=set(window)
        missing=due_set-got
        if (len(window)!=n or set(required)!=got or a["halted"]):
            ready=False
        coverage.append({
            "lane":lane,"symbol":a["symbol"],"strategy":a["label"],
            "due_dates":due,"recorded_due_dates":len(due_set & got),
            "missing_due_dates":len(missing),
            "halted":bool(a["halted"]),
            "first_missing":min(missing) if missing else None,
        })
    result={
        "status":"WAITING_FOR_FUTURE_CUTOFF" if current<opening else
                 "INCOMPLETE_OR_NOT_COMPARABLE",
        "protocol_sha256":protocol["sha256"],
        "window":[start.isoformat(),end.isoformat()],
        "evaluation_not_before_utc":opening.isoformat(),
        "registered_accounts":len(all_accounts),
        "due_dates_per_account":due,
        "coverage":coverage,
        "issues":sorted(set(issues)),
        "net_return_by_strategy":None,
        "same_day_cost_and_price_comparison":False,
        "blinded_or_confirmatory":False,
        "real_orders_enabled":False,
        "automatic_strategy_promotion":False,
    }
    if issues:
        result["status"]="INTEGRITY_REVIEW_REQUIRED"
        return result
    if current<opening:
        return result
    if not ready:
        result["issues"].append("MISSING_90_DAY_OBSERVATIONS_OR_RISK_HALT")
        return result
    # All three exact same-date windows must use materially equivalent public
    # ticker references. Otherwise no apples-to-apples hypothetical PnL table.
    price_divergence={}
    for symbol in ("KRW-BTC","KRW-ETH"):
        maximum=0.
        for day in required:
            px=[]
            for lane,a in count_by_sym[symbol]:
                event=next(e for e in events_by_lane[lane][a["track_id"]]
                           if e["decision_date"]==day)
                quote=quote_by_lane[lane].get((symbol,day))
                if (quote is None or
                    not math.isclose(float(event["observed_price"]),
                      float(quote["public_ticker_price"]),rel_tol=1e-10,abs_tol=1e-7) or
                    quote["session_date"]!=event["session_date"]):
                    result["issues"].append("MISSING_OR_CONTRADICTORY_PUBLIC_QUOTE")
                    return result
                px.append(float(event["observed_price"]))
            maximum=max(maximum,(max(px)/min(px)-1)*10000)
        price_divergence[symbol]=round(maximum,4)
    result["max_interlane_public_ticker_divergence_bps"]=price_divergence
    if any(v>price_tolerance_bps for v in price_divergence.values()):
        result["issues"].append("DIFFERENT_OBSERVATION_PRICE_BASIS")
        return result
    by_symbol_benchmark={}
    scored=[]
    for lane,a in all_accounts:
        rows=events_by_lane[lane][a["track_id"]]
        previous=[e for e in rows if e["decision_date"]<start.isoformat()]
        opening_equity=float(previous[-1]["equity"]) if previous else float(a["initial_cash"])
        final=next(e for e in rows if e["decision_date"]==end.isoformat())
        observed=float(final["equity"])/opening_equity-1
        line={"lane":lane,"symbol":a["symbol"],"strategy":a["label"],
              "observed_90d_net_return":observed,
              "simulated_orders":sum(
                  e["action"] in ("BUY","SELL") for e in rows
                  if start.isoformat()<=e["decision_date"]<=end.isoformat()),
              "modelled_trading_cost_krw":sum(
                  float(e["simulated_cost"]) for e in rows
                  if start.isoformat()<=e["decision_date"]<=end.isoformat())}
        scored.append(line)
        if lane=="registered_90d" and a["label"]=="buy_hold_20pct":
            by_symbol_benchmark[a["symbol"]]=observed
    if set(by_symbol_benchmark)!={"KRW-BTC","KRW-ETH"}:
        result["issues"].append("MISSING_FROZEN_SAME_EXPOSURE_BENCHMARK")
        return result
    for row in scored:
        row["excess_vs_same_symbol_buy_hold_pp"]=100*(
            row["observed_90d_net_return"]-by_symbol_benchmark[row["symbol"]])
    result["status"]="DESCRIPTIVE_MANUAL_REVIEW_ONLY"
    result["net_return_by_strategy"]=scored
    result["same_day_cost_and_price_comparison"]=True
    result["warning"]="Not blinded or confirmatory; historical candidate selection and "
    result["warning"]+="hypothetical public-ticker fills mean no claim of executable alpha."
    return result


def main(argv=None):
    import argparse
    import json
    parser=argparse.ArgumentParser(
        description="Read-only future 90-day three-lane comparison; no order placement")
    parser.add_argument("--production-db",default="state/shadow.db")
    parser.add_argument("--agent-db",default="state/agent_lab_shadow.db")
    parser.add_argument("--qlib-db",default="state/qlib_lab_shadow.db")
    parser.add_argument("--protocol",default="protocols/2026-10-20-btc-eth-90d.json")
    parser.add_argument("--agent-plan",default="protocols/2026-10-20-exaone-agent-lab.json")
    parser.add_argument("--qlib-plan",default="protocols/2026-10-20-qlib-lab.json")
    args=parser.parse_args(argv)
    try:
        r=compare_three_lanes(args.production_db,args.agent_db,args.qlib_db,
            args.protocol,args.agent_plan,args.qlib_plan)
        print(json.dumps(r,ensure_ascii=False,indent=2,allow_nan=False))
        return 2 if r["status"]=="INTEGRITY_REVIEW_REQUIRED" else 0
    except (OSError,ValueError,KeyError,RuntimeError) as exc:
        import sys
        print("READ-ONLY THREE-LANE COMPARISON BLOCKED: "+str(exc),file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

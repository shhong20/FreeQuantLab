"""Read-only conditional fill-scenario study on SHA-pinned KRX halt-aware 5m data.

NOT a broker-fill backtest. This calculates *conditional* hypothetical returns
only under an explicitly unverified 'bar label = start' and 'next bar OPEN
fully fills' assumption. Blocks entire risky stock-days and retains them in
the 100-session denominator. Never changes operating databases or orders.
"""
from __future__ import annotations
from collections import Counter
from hashlib import sha256
import argparse
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import tempfile
from statistics import median

from .intraday_krx_halt_position_stress import _read_verified, _validated
from .intraday_krx_signal_audit import CLEAN
from .intraday_kis_venues import VenueProbeError

STRATEGIES = ("orb_5m_rvol1.5", "orb_15m_rvol1.5", "orb_30m_rvol1.5",
              "intraday_momentum_30m")
FAMILIES = {"orb":STRATEGIES[:3], "intraday_momentum":STRATEGIES[3:]}
INITIAL_CASH = 10_000_000
ASSUMPTION = "UNVERIFIED_BAR_START_AND_NEXT_BAR_OPEN_FULL_FILL"
TAX_BPS_KOSPI_2026 = 20.0
MIN_VALIDATION_TRADES = 3
MIN_TEST_TRADES = 10


def _signed_cost_return(entry: float, exit_: float, quantity: int,
                        *, fee: float, slip: float, tax: float) -> float:
    if not (math.isfinite(entry) and math.isfinite(exit_) and
            entry > 0 and exit_ > 0 and quantity > 0):
        raise VenueProbeError("Invalid conditional execution price")
    buy = entry * (1 + (fee + slip) / 10000)
    sell = exit_ * (1 - (fee + slip + tax) / 10000)
    return quantity * (sell - buy) / INITIAL_CASH


def _metrics(sessions: list[str], rows: list[dict], cost_key: str) -> dict:
    trades = {r["session"]:r for r in rows if r["session"] in sessions}
    if len(trades) != len([r for r in rows if r["session"] in sessions]):
        raise VenueProbeError("Duplicate candidate trades in same day")
    # Zero represents a FLAT cash day, never a fabricated 5m bar.
    day_returns = [trades[s][cost_key] if s in trades else 0.0 for s in sessions]
    equity=1.0
    peak=1.0
    dd=0.0
    for value in day_returns:
        equity *= 1.0+value
        peak=max(peak,equity)
        dd=max(dd, 1.0-equity/peak)
    n=len(day_returns)
    avg=sum(day_returns)/n
    sd=math.sqrt(sum((x-avg)**2 for x in day_returns)/(n-1)) if n>1 else 0.0
    return {
        "population_days_including_blocked":n,
        "hypothetical_trades":len(trades),
        "hypothetical_compounded_return":equity-1.0,
        "hypothetical_max_drawdown":dd,
        "hypothetical_sharpe_daily_annualized":avg/sd*math.sqrt(252) if sd else None,
        "positive_trade_fraction":sum(r[cost_key]>0 for r in trades.values())/len(trades)
        if trades else None,
        "assumed_fill_not_observed":True,
        "certified_strategy_performance":False,
    }


def _simulate_day(*, symbol: str, session: str, bars: list[dict],
                  history: dict[int,list[int]], fee:float,slip:float,
                  tax:float) -> list[dict]:
    choices=[]
    for n in (1,3,6):
        prior=history[n][-20:]
        first_volume=sum(x["volume"] for x in bars[:n])
        if len(prior)>=10 and median(prior)>0 and (
            first_volume>=1.5*median(prior)
        ):
            high=max(x["high"] for x in bars[:n])
            for idx in range(n,len(bars)-2):
                if bars[idx]["close"]>high:
                    choices.append((f"orb_{n*5}m_rvol1.5",idx))
                    break
    # First 6 completed 5m candles -> next candle open, not same-bar close.
    if bars[5]["close"]/bars[0]["open"]-1.0>=.002:
        choices.append(("intraday_momentum_30m",5))
    for n in history:
        history[n].append(sum(x["volume"] for x in bars[:n]))
    trades=[]
    for name, signal in choices:
        entry_ix=signal+1
        last_ix=75
        if entry_ix>=last_ix:
            continue
        entry=float(bars[entry_ix]["open"])
        end=float(bars[last_ix]["open"])
        # Volume is NOT used from the entry/exit bar as an oracle for
        # earlier size decisions. Risk sizing uses only the signal bar.
        evidence_volume=int(bars[signal]["volume"])
        max_prior_volume_shares=math.floor(evidence_volume*0.01)
        purchase_unit=entry*(1+(fee+slip)/10000)
        quantity=min(math.floor(INITIAL_CASH/purchase_unit),
                     max_prior_volume_shares)
        if quantity<1:
            continue
        trades.append({
            "symbol":symbol,"session":session,"strategy":name,
            "signal_slot_start_kst":f"{session}T{(9*60+signal*5)//60:02d}:{(9*60+signal*5)%60:02d}:00+09:00",
            "hypothetical_entry_slot_start_kst":f"{session}T{(9*60+entry_ix*5)//60:02d}:{(9*60+entry_ix*5)%60:02d}:00+09:00",
            "hypothetical_exit_slot_start_kst":f"{session}T15:15:00+09:00",
            "entry_open":entry, "exit_open":end,"assumed_shares":quantity,
            "sizing_volume_prior_completed_signal_bar":evidence_volume,
            "hypothetical_net_return_base":_signed_cost_return(
                entry,end,quantity,fee=fee,slip=slip,tax=tax),
            "hypothetical_net_return_double_friction":_signed_cost_return(
                entry,end,quantity,fee=fee*2,slip=slip*2,tax=tax),
            "fill_observed":False, "order_submitted":False,
        })
    return trades



def _prehalt_momentum_exposure(day: dict) -> dict | None:
    """Potential position before a reported halt, not a demonstrated fill.

    Unlike the calendar-day risk gate this probes observations only available
    prior to the reported activation time. It leaves any potential exposure
    unvalued, not silently turned into a cash-day realized return.
    """
    event=day.get("reported_cb")
    if not event:
        return None
    activated=datetime.fromisoformat(event["activation_kst"])
    slots=day["slots"]
    first=slots[:7]
    if len(first)!=7 or any(
        x.get("ohlcv") is None or
        x.get("source_observed_count")!=5 or
        x.get("market_reported_cb_window_overlap") is not False
        for x in first
    ):
        return {"status":"PRE_HALT_EARLY_BAR_COVERAGE_UNCERTAIN",
                "potential_position_at_halt":None,
                "realized_pnl":None}
    signal_known_at=datetime.fromisoformat(first[5]["timestamp"])+timedelta(minutes=5)
    next_open=datetime.fromisoformat(first[6]["timestamp"])
    if signal_known_at>activated or next_open>=activated:
        return {"status":"HALT_PRECEDES_30M_SIGNAL_ENTRY",
                "potential_position_at_halt":False,
                "realized_pnl":None}
    momentum=(first[5]["ohlcv"]["close"]/first[0]["ohlcv"]["open"])-1
    possible=momentum>=.002
    return {"status":("POSSIBLE_PRE_HALT_LONG_EXPOSURE_UNPRICED"
                     if possible else "NO_30M_MOMENTUM_SIGNAL_BEFORE_HALT"),
            "potential_position_at_halt":possible,
            "hypothetical_signal":possible,
            "activation_kst":event["activation_kst"],
            "signal_complete_at_kst":signal_known_at.isoformat(),
            "earliest_assumed_entry_at_kst":next_open.isoformat(),
            "realized_pnl":None}



def evaluate(*, input_root: str|Path, stress_root: str|Path,
             out_dir: str|Path, fee_bps:float=3.0,
             slippage_bps:float=5.0,
             sell_tax_bps:float=TAX_BPS_KOSPI_2026) -> dict:
    source=Path(input_root).expanduser().resolve()
    risk=Path(stress_root).expanduser().resolve()
    target=Path(out_dir).expanduser().resolve()
    if target.exists():
        raise VenueProbeError("Immutable conditional scenario report already exists")
    if (any(not isinstance(x,(int,float)) or isinstance(x,bool) or
            not math.isfinite(x) or x<0 for x in
            (fee_bps,slippage_bps,sell_tax_bps)) or
        2*(fee_bps+slippage_bps)+sell_tax_bps>=10000):
        raise VenueProbeError("Invalid execution cost assumptions")
    summary=_read_verified(source,"halt_5m_summary.json")
    if (not isinstance(summary,dict) or
        summary.get("status")!="HALT_AWARE_5M_CANDIDATES_RESEARCH_ONLY_NO_PNL" or
        summary.get("usable_for_backtesting") is not False or
        summary.get("usable_for_paper_or_live") is not False):
        raise VenueProbeError("Require read-only risk-gated KRX source")
    symbols=summary.get("symbols")
    sessions=summary.get("sessions")
    if (not isinstance(symbols,list) or not 1<=len(symbols)<=3 or
        len(set(symbols))!=len(symbols) or
        not isinstance(sessions,list) or not 60<=len(sessions)<=120 or
        sessions!=sorted(set(sessions)) or
        summary.get("total_stock_days")!=len(symbols)*len(sessions) or
        summary.get("total_5m_clock_slots")!=76*len(symbols)*len(sessions)):
        raise VenueProbeError("Chronological complete stock-day population required")
    digest=sha256((source/"halt_5m_summary.json").read_bytes()).hexdigest()
    stress=_read_verified(risk,"stress_report.json")
    if (not isinstance(stress,dict) or
        stress.get("status")!="HYPOTHETICAL_HALT_ORDER_POLICY_AND_GAP_OBSERVATION_NO_PNL" or
        stress.get("source_summary_sha256")!=digest or
        stress.get("source_stock_days")!=len(symbols)*len(sessions) or
        stress.get("usable_for_backtesting") is not False or
        stress.get("positions_never_automatically_liquidated_at_missing_bar") is not True or
        stress.get("unfilled_orders_never_assumed_executed_on_reopening") is not True):
        raise VenueProbeError("Halt-hold risk replay must be pinned and no-fill")
    risk_cases=stress.get("halt_exposure_scenarios")
    if not isinstance(risk_cases,list) or len(risk_cases)!=stress.get("scenario_count"):
        raise VenueProbeError("Halt position stress population missing")
    for case in risk_cases:
        if (case.get("hypothetical_final_held_shares") !=
            case.get("hypothetical_initial_held_shares") or
            case["hypothetical_exit_order_at_activation"].get("filled_shares") != 0 or
            case["post_halt_entry_or_exit"].get("filled_shares") != 0):
            raise VenueProbeError("Halt position incorrectly liquidated")
    b1,b2=int(.6*len(sessions)),int(.8*len(sessions))
    windows={"training":sessions[:b1], "validation":sessions[b1:b2],
             "untouched_test":sessions[b2:]}
    if any(not v for v in windows.values()):
        raise VenueProbeError("Missing precommitted chronological split")
    reported=Counter()
    by_symbol={}
    day_states=[]
    prehalt_diagnostics=[]
    for symbol in symbols:
        dataset=_validated(source,symbol,sessions)
        baseline={n:[] for n in (1,3,6)}
        strategy_trades={k:[] for k in STRATEGIES}
        blocked=Counter()
        for day in dataset:
            reason=day["day_disposition"]
            reported[reason]+=1
            eligible=reason==CLEAN and day["reported_cb"] is None
            slots=day["slots"]
            if len(slots)!=76:
                raise VenueProbeError("Missing 5m clock slot")
            day_states.append({"symbol":symbol,"session":day["session"],
                               "disposition":reason, "assumed_entries_allowed":eligible,
                               "full_5m_slots_preserved":76,
                               "actual_orders":0, "realized_pnl":None})
            if day.get("reported_cb") is not None:
                prehalt_diagnostics.append({
                    "symbol":symbol,"session":day["session"],
                    "momentum":_prehalt_momentum_exposure(day)})
            if not eligible:
                blocked[reason]+=1
                continue
            if (day.get("closing_153000_label_present") is not True or
                any(s["ohlcv"] is None or s.get("source_observed_count")!=5 or
                    s.get("market_reported_cb_window_overlap") is not False
                    for s in slots)):
                raise VenueProbeError("Eligible day has unsafe incomplete 5m bars")
            bars=[s["ohlcv"] for s in slots]
            for trade in _simulate_day(symbol=symbol,session=day["session"],
                                       bars=bars,history=baseline,
                                       fee=fee_bps,slip=slippage_bps,tax=sell_tax_bps):
                strategy_trades[trade["strategy"]].append(trade)
        validation={}
        for key in STRATEGIES:
            validation[key]=_metrics(windows["validation"],strategy_trades[key],
                                     "hypothetical_net_return_base")
        selected={}
        untouched={}
        for family,names in FAMILIES.items():
            eligible_names=[x for x in names
                            if validation[x]["hypothetical_trades"]>=MIN_VALIDATION_TRADES]
            chosen=max(eligible_names,key=lambda x:(
                validation[x]["hypothetical_compounded_return"],-names.index(x))
                       ) if eligible_names else None
            selected[family]=chosen
            if chosen is None:
                untouched[family]={"status":"NO_ELIGIBLE_VALIDATION_STRATEGY"}
                continue
            base=_metrics(windows["untouched_test"],strategy_trades[chosen],
                          "hypothetical_net_return_base")
            double=_metrics(windows["untouched_test"],strategy_trades[chosen],
                            "hypothetical_net_return_double_friction")
            untouched[family]={
                "status":("CONDITIONAL_SCENARIO_MINIMUM_TRADES_MET"
                          if base["hypothetical_trades"]>=MIN_TEST_TRADES else
                          "DIAGNOSTIC_ONLY_INSUFFICIENT_TEST_TRADES"),
                "selected_variant":chosen,
                "assumed_cost_scenario":base,
                "double_fee_slippage_scenario":double,
                "strategy_profit_proven":False,
            }
        by_symbol[symbol]={
            "source_stock_days":len(dataset),
            "blocked_stock_days":sum(blocked.values()),
            "blocked_dispositions":dict(blocked),
            "validation_variants":validation,
            "chosen_from_validation_only":selected,
            "untouched_test_selected_only":untouched,
            "signal_and_hypothetical_trade_log":strategy_trades,
        }
    if dict(reported)!=summary.get("day_dispositions"):
        raise VenueProbeError("Observed stock-days do not match source summary")
    if len(risk_cases) != sum(1 for d in summary.get("day_records_without_slots",[])
                            if d.get("reported_cb") is not None):
        raise VenueProbeError("Halt exposures missing from coupled audit")
    result={
        "status":"CONDITIONAL_KRX_OPEN_FILL_SCENARIO_NOT_VALIDATED_BACKTEST",
        "source_summary_sha256":digest,
        "held_position_stress_sha256":sha256(
            (risk/"stress_report.json").read_bytes()).hexdigest(),
        "symbols":symbols,"sessions":sessions,
        "precommitted_windows":{k:[v[0],v[-1],len(v)] for k,v in windows.items()},
        "day_dispositions":dict(reported),
        "all_stock_days_preserved":len(day_states),
        "risk_day_rows":day_states,
        "stress_halted_stock_day_count":len(risk_cases),
        "prehalt_momentum_exposure_diagnostics":prehalt_diagnostics,
        "potential_prehalt_momentum_exposure_stock_days":sum(
            row["momentum"].get("potential_position_at_halt") is True
            for row in prehalt_diagnostics),
        "risk_positions_stay_held_if_already_owned":True,
        "no_new_entries_whole_day_if_halt_gap_or_unverified_close":True,
        "whole_day_halt_avoidance_uses_future_day_disposition":True,
        "whole_day_risk_gate_is_oracular_not_live_implementable":True,
        "held_position_scenario_cannot_bound_unknown_reopen_fills":True,
        "historical_test_signal_counts_were_previously_disclosed":True,
        "holdout_untouched_in_full_information_sense":False,
        "open_fill_and_clock_assumption":ASSUMPTION,
        "fee_bps_each_side":fee_bps,"slippage_bps_each_side":slippage_bps,
        "sell_tax_bps":sell_tax_bps,
        "sell_tax_source":"KRX_KOSPI_2026_0.05PCT_STT_PLUS_0.15PCT_RURAL_TAX",
        "double_friction_keeps_sell_tax_unchanged":True,
        "costs_and_entry_exit_prices_hypothetical":True,
        "corporate_actions_verified":False,
        "official_halt_clocks_verified":False,
        "vendor_minute_start_end_semantics_verified":False,
        "broker_fills_verified":False,
        "last_5m_exit_open_is_not_official_close":True,
        "cash_reset_each_day_no_overnight_positions_in_strategy_scenario":True,
        "preexisting_halted_positions_are_stress_only_no_realized_return":True,
        "gap_reversal_disabled_no_verified_auction_and_actions":True,
        "by_symbol":by_symbol,
        "real_orders_executed":0,
        "strategy_profit_proven":False,
        "usable_for_backtesting":False,
        "usable_for_paper_or_live":False,
    }
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=Path(tempfile.mkdtemp(prefix=".krx-open-scenario-",dir=target.parent))
    try:
        raw=(json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        (temp/"scenario_report.json").write_bytes(raw)
        (temp/"scenario_report.sha256").write_text(sha256(raw).hexdigest()+"\n")
        if target.exists():
            raise VenueProbeError("Concurrent immutable KRX scenario report")
        temp.rename(target)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return result


def main(argv: list[str]|None=None) -> int:
    p=argparse.ArgumentParser(description="Read-only CONDITIONAL KRX scenario, NOT actual fills")
    p.add_argument("--input-root",required=True)
    p.add_argument("--stress-root",required=True)
    p.add_argument("--out-dir",required=True)
    p.add_argument("--fee-bps",type=float,default=3.0)
    p.add_argument("--slippage-bps",type=float,default=5.0)
    p.add_argument("--sell-tax-bps",type=float,default=TAX_BPS_KOSPI_2026)
    args=p.parse_args(argv)
    try:
        report=evaluate(input_root=args.input_root,stress_root=args.stress_root,
                        out_dir=args.out_dir,fee_bps=args.fee_bps,
                        slippage_bps=args.slippage_bps,sell_tax_bps=args.sell_tax_bps)
        print(json.dumps({"status":report["status"],
                          "all_stock_days_preserved":report["all_stock_days_preserved"],
                          "halt_risk_episodes":report["stress_halted_stock_day_count"],
                          "results":{s:{"selection":v["chosen_from_validation_only"],
                                        "test":v["untouched_test_selected_only"]}
                                     for s,v in report["by_symbol"].items()}},
                          sort_keys=True,ensure_ascii=False,indent=2))
        return 0
    except (VenueProbeError,ValueError,TypeError,KeyError,OverflowError):
        p.exit(2,"FAIL CLOSED: insufficient verified KRX scenario provenance\n")


if __name__=="__main__":
    raise SystemExit(main())

"""Point-in-time ordered, *hypothetical* KRX 5m execution/CB state replay.

No advance knowledge of a day's halt or missing minutes enters signal/order
decisions. Event times are reported after-the-fact, NOT an authenticated live
feed; five-minute timestamp semantics, bid/ask and fills remain unverified.
Positions facing halt/gap are frozen, NEVER liquidated at a fictitious price.
A frozen position carries across days until externally reconciled.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
from statistics import median
import tempfile

from .intraday_krx_halt_position_stress import _read_verified, _validated
from .intraday_krx_halt_scenario import (
    ASSUMPTION, INITIAL_CASH, TAX_BPS_KOSPI_2026,
)
from .intraday_kis_venues import VenueProbeError

NAMES = ("orb_5m_rvol1.5", "orb_15m_rvol1.5", "orb_30m_rvol1.5",
         "intraday_momentum_30m")
LOOKBACK = (1, 3, 6)
FIELDS = ("open", "high", "low", "close", "volume")


def _costs(fee: float, slip: float, tax: float) -> None:
    if (any(not isinstance(v, (int, float)) or isinstance(v, bool) or
            not math.isfinite(v) or v < 0 for v in (fee, slip, tax)) or
            2*(fee+slip)+tax >= 10000):
        raise VenueProbeError("Invalid nonnegative simulation cost")


def _available(bar: dict) -> bool:
    return (bar.get("ohlcv") is not None and
            bar.get("source_observed_count") == 5 and
            bar.get("market_reported_cb_window_overlap") is False and
            bar.get("state") == "FULL_OBSERVED_5M_PROVISIONAL")


def _candidate_signal(name: str, i: int, prices: list[dict | None],
                      history: dict[int, list[float]]) -> bool:
    """Only fully completed observations through index i are visible."""
    if i >= 74 or prices[i] is None:
        return False
    if name == "intraday_momentum_30m":
        return i == 5 and all(prices[j] is not None for j in range(6)) and (
            prices[5]["close"] / prices[0]["open"] >= 1.002
        )
    n = {"orb_5m_rvol1.5": 1, "orb_15m_rvol1.5": 3,
         "orb_30m_rvol1.5": 6}[name]
    if i < n or any(prices[k] is None for k in range(n)):
        return False
    prior = history[n][-20:]
    if len(prior) < 10 or median(prior) <= 0:
        return False
    opening_volume = sum(prices[k]["volume"] for k in range(n))
    return (opening_volume >= 1.5*median(prior) and
            prices[i]["close"] > max(prices[k]["high"] for k in range(n)))


def _replay_one(name: str, days: list[dict], *,
                fee: float, slip: float, tax: float,
                windows: dict[str, str]) -> dict:
    history = {k: [] for k in LOOKBACK}
    position = None
    frozen = None
    trades = []
    events = []
    by_window = {k: Counter() for k in ("train", "validation", "test")}
    for day in days:
        session = day["session"]
        window = windows[session]
        ev = day["reported_cb"]
        activation = datetime.fromisoformat(ev["activation_kst"]) if ev else None
        blackout = datetime.fromisoformat(
            ev["conservative_continuous_order_blackout_until_kst"]
        ) if ev else None
        bars = day["slots"]
        prices: list[dict | None] = [None]*76
        pending = None
        fired = False
        # No day_disposition / future missing status in the event loop.
        halted = False
        gap_seen = False
        for i, slot in enumerate(bars):
            start = datetime.fromisoformat(slot["timestamp"])
            end = start + timedelta(minutes=5)
            complete = _available(slot)
            overlap = bool(activation and start < blackout and end > activation)
            if position is not None or frozen is not None:
                # Already in a position? Apply event on arrival, not at day open.
                if (overlap or (activation and start >= activation and
                                start < blackout)):
                    if frozen is None:
                        frozen = {
                            "reason": "POSITION_OVERLAPS_REPORTED_CB",
                            "symbol": day["symbol"], "session":session,
                            "first_unknown_at":slot["timestamp"],
                            "entry":position,
                            "realized_pnl":None,
                        }
                        events.append({"session":session, "slot":i,
                                       "event":"POSITION_FROZEN_AT_CB",
                                       "order_executed":False})
                    position = None
            if overlap:
                halted = True
                if pending and frozen is None:
                    # Pending before the halt; not safe to assert that it
                    # never filled at the open of this partial 5m slot.
                    frozen = {"reason": "PENDING_ENTRY_OVERLAPS_CB_UNCERTAIN",
                              "symbol":day["symbol"], "session":session,
                              "first_unknown_at":slot["timestamp"],
                              "entry":None, "realized_pnl":None}
                    events.append({"session":session, "slot":i,
                                   "event":"AMBIGUOUS_ENTRY_HALT_OVERLAP",
                                   "order_executed":False})
                pending = None
            if not complete:
                gap_seen = True
                if position is not None and frozen is None:
                    frozen = {"reason":("POSITION_AT_UNVERIFIED_CB" if overlap
                                       else "POSITION_AT_UNKNOWN_SOURCE_GAP"),
                              "symbol":day["symbol"], "session":session,
                              "first_unknown_at":slot["timestamp"],
                              "entry":position,"realized_pnl":None}
                    events.append({"session":session, "slot":i,
                                   "event":"POSITION_FROZEN_MISSING_BAR",
                                   "order_executed":False})
                    position = None
                if pending and frozen is None and not overlap:
                    frozen = {"reason":"PENDING_ENTRY_ON_MISSING_BAR",
                              "symbol":day["symbol"],"session":session,
                              "first_unknown_at":slot["timestamp"],
                              "entry":None,"realized_pnl":None}
                    events.append({"session":session,"slot":i,
                                   "event":"AMBIGUOUS_ENTRY_MISSING_BAR",
                                   "order_executed":False})
                pending = None
                continue
            # Reopening never resumes an already frozen entry or auto-sells.
            prices[i] = slot["ohlcv"]
            if frozen is not None:
                pending = None
                continue
            if halted:
                pending = None
                continue
            # When an independently unknown gap has occurred, do not assume
            # that trading has resumed or an order can be filled that day.
            if gap_seen:
                pending = None
                continue
            # For fills based on bar start: only act on a prior bar signal.
            if pending == i and position is None:
                px = prices[i]["open"]
                evidence = int(prices[i-1]["volume"])
                qty = min(math.floor(INITIAL_CASH/(px*(1+(fee+slip)/10000))),
                          math.floor(evidence*0.01))
                if qty > 0:
                    position = {
                        "session":session, "open_slot":i,
                        "entry_price_assumed":px, "shares_assumed":qty,
                        "signal_completed_at":(start-timedelta(minutes=0)).isoformat(),
                        "fee_slip_bps_each_side":fee+slip,
                    }
                    by_window[window]["conditional_entries"] += 1
                    events.append({"session":session,"slot":i,
                                   "event":"HYPOTHETICAL_NEXT_OPEN_ENTRY",
                                   "shares":qty,"price":px,
                                   "order_executed":False})
                pending = None
            if i == 75 and position is not None:
                entry = position
                exit_px = prices[i]["open"]
                quantity = entry["shares_assumed"]
                buy = entry["entry_price_assumed"]*(1+(fee+slip)/10000)
                sell = exit_px*(1-(fee+slip+tax)/10000)
                ret = quantity*(sell-buy)/INITIAL_CASH
                trades.append({"session":session,"symbol":day["symbol"],
                               "strategy":name,"entry":entry,
                               "hypothetical_exit_slot_start":slot["timestamp"],
                               "hypothetical_exit_price":exit_px,
                               "conditional_cash_return":ret,
                               "actual_fill":False})
                by_window[window]["hypothetical_closed"] += 1
                events.append({"session":session,"slot":i,
                               "event":"HYPOTHETICAL_CLOSE_BEFORE_AUCTION",
                               "order_executed":False})
                position = None
            # Only after the current bar is complete may it create a signal.
            # Prevent a signal from being generated at or after activation.
            if (position is None and not fired and i < 74 and
                (activation is None or end <= activation) and
                _candidate_signal(name, i, prices, history)):
                pending = i+1
                fired = True
                by_window[window]["completed_bar_signals"] += 1
                events.append({"session":session, "slot":i,
                               "event":"SIGNAL_AFTER_COMPLETED_BAR",
                               "order_executed":False})
        # The previous 5m day's close does NOT certify a 15:30 auction.
        # Any unclosed position must stay held for the next session.
        if position is not None:
            frozen = {"reason":"EXIT_UNEXECUTABLE_AT_SESSION_END",
                      "symbol":day["symbol"],"session":session,
                      "first_unknown_at":bars[-1]["timestamp"],
                      "entry":position,"realized_pnl":None}
            position=None
        if not gap_seen and all(p is not None for p in prices[:6]):
            for n in LOOKBACK:
                history[n].append(sum(prices[j]["volume"] for j in range(n)))
        by_window[window]["sessions_seen"] += 1
        by_window[window]["cb_day_observed_ex_post"] += int(ev is not None)
        by_window[window]["incomplete_day_observed_ex_post"] += int(gap_seen)
        by_window[window]["frozen_at_day_end"] += int(frozen is not None)
    if position is not None:
        raise VenueProbeError("Unclosed position not carried forward")
    return {
        "strategy":name, "source_sessions":len(days),
        "final_state": "FROZEN_UNRECONCILED" if frozen else "FLAT",
        "frozen_exposure":frozen,
        "reconciliation_required_before_new_trades":frozen is not None,
        "closed_hypothetical_trades":trades,
        "events":events, "window_counts":{k:dict(v) for k,v in by_window.items()},
        "full_period_realized_return":None,
        "full_period_mdd":None,
        "full_period_sharpe":None,
        "order_submissions":0,
    }


def replay(*, source_root: str | Path, stress_root: str | Path,
           output_dir: str | Path, fee_bps: float=3.0,
           slippage_bps: float=5.0,
           sell_tax_bps: float=TAX_BPS_KOSPI_2026) -> dict:
    _costs(fee_bps, slippage_bps, sell_tax_bps)
    source = Path(source_root).expanduser().resolve()
    stress_dir = Path(stress_root).expanduser().resolve()
    dest = Path(output_dir).expanduser().resolve()
    if dest.exists():
        raise VenueProbeError("Immutable PIT replay output already exists")
    summary = _read_verified(source, "halt_5m_summary.json")
    if (not isinstance(summary, dict) or
        summary.get("status") != "HALT_AWARE_5M_CANDIDATES_RESEARCH_ONLY_NO_PNL" or
        summary.get("usable_for_backtesting") is not False or
        summary.get("usable_for_paper_or_live") is not False):
        raise VenueProbeError("PIT study requires quarantined 5m source")
    sessions=summary.get("sessions")
    symbols=summary.get("symbols")
    if (not isinstance(sessions,list) or not 60<=len(sessions)<=120 or
        sessions != sorted(set(sessions)) or
        not isinstance(symbols,list) or not 1<=len(symbols)<=3 or
        len(set(symbols))!=len(symbols) or
        summary.get("total_stock_days")!=len(sessions)*len(symbols) or
        summary.get("total_5m_clock_slots")!=76*len(sessions)*len(symbols)):
        raise VenueProbeError("Invalid immutable session population")
    digest=sha256((source/"halt_5m_summary.json").read_bytes()).hexdigest()
    stress=_read_verified(stress_dir,"stress_report.json")
    if (not isinstance(stress,dict) or
        stress.get("source_summary_sha256")!=digest or
        stress.get("status")!="HYPOTHETICAL_HALT_ORDER_POLICY_AND_GAP_OBSERVATION_NO_PNL" or
        stress.get("source_stock_days")!=len(sessions)*len(symbols) or
        stress.get("usable_for_backtesting") is not False or
        stress.get("usable_for_paper_or_live") is not False or
        stress.get("positions_never_automatically_liquidated_at_missing_bar") is not True or
        stress.get("unfilled_orders_never_assumed_executed_on_reopening") is not True):
        raise VenueProbeError("Source and held-position stress mismatch")
    expected_halt_count=sum(
        1 for row in summary.get("day_records_without_slots",[])
        if isinstance(row,dict) and row.get("reported_cb") is not None
    )
    scenarios=stress.get("halt_exposure_scenarios")
    if (not isinstance(scenarios,list) or
        len(scenarios)!=expected_halt_count or
        stress.get("scenario_count")!=len(scenarios) or
        any(not isinstance(s,dict) or
            s.get("hypothetical_initial_held_shares") !=
                s.get("hypothetical_final_held_shares") or
            s.get("hypothetical_exit_order_at_activation",{}).get("filled_shares")!=0 or
            s.get("post_halt_entry_or_exit",{}).get("filled_shares")!=0
            for s in scenarios)):
        raise VenueProbeError("Altered stress replay could invent halt fills")
    a,b=int(.6*len(sessions)),int(.8*len(sessions))
    windows={s:("train" if i<a else "validation" if i<b else "test")
             for i,s in enumerate(sessions)}
    data={}
    disposition=Counter()
    for symbol in symbols:
        days=_validated(source,symbol,sessions)
        for d in days:
            disposition[d["day_disposition"]]+=1
        data[symbol] = {
            strategy:_replay_one(strategy,days,fee=fee_bps,
                                  slip=slippage_bps,tax=sell_tax_bps,
                                  windows=windows)
            for strategy in NAMES
        }
    if dict(disposition)!=summary.get("day_dispositions"):
        raise VenueProbeError("Original population was silently excluded")
    payload={
        "schema":"KRX_PIT_ORDERED_5M_HALT_STATE_REPLAY_V1",
        "status":"PIT_RESEARCH_PROVISIONAL_NO_CERTIFIED_FILLS_OR_PNL",
        "source_summary_sha256":digest,
        "source_stress_sha256":sha256(
            (stress_dir/"stress_report.json").read_bytes()).hexdigest(),
        "population_stock_days":len(sessions)*len(symbols),
        "all_session_dates_preserved":sessions,
        "day_dispositions_ex_post_only":dict(disposition),
        "by_symbol_strategy":data,
        "conditional_bar_start_next_open_assumption":ASSUMPTION,
        "risk_events_are_public_reports_not_live_event_feed":True,
        "day_level_halt_blacklist_used_in_decision_loop":False,
        "reported_event_delivery_time_unverified":True,
        "conditional_next_open_zero_latency_assumption_unverified":True,
        "training_validation_test_windows_are_not_blind_after_prior_exposure":True,
        "past_bar_only_signals":True,
        "unreconciled_positions_never_closed_automatically":True,
        "all_orders_hypothetical":True,
        "real_orders_executed":0,
        "vendor_timestamp_contract_verified":False,
        "corporate_actions_verified":False,
        "broker_fills_verified":False,
        "certified_pnl_or_sharpe_calculated":False,
        "paper_or_live_enabled":False,
        "usable_for_backtesting":False,
    }
    dest.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".krx-pit-",dir=dest.parent))
    try:
        blob=(json.dumps(payload,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        (stage/"pit_replay.json").write_bytes(blob)
        (stage/"pit_replay.sha256").write_text(sha256(blob).hexdigest()+"\n")
        if dest.exists():
            raise VenueProbeError("Concurrent immutable PIT report")
        stage.rename(dest)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return payload


def main(argv: list[str]|None=None) -> int:
    parser=argparse.ArgumentParser(description="KRX point-in-time research: never orders")
    parser.add_argument("--source-root",required=True)
    parser.add_argument("--stress-root",required=True)
    parser.add_argument("--output-dir",required=True)
    args=parser.parse_args(argv)
    try:
        result=replay(source_root=args.source_root,stress_root=args.stress_root,
                      output_dir=args.output_dir)
        print(json.dumps({
            "status":result["status"],
            "population_stock_days":result["population_stock_days"],
            "study_counts":{
                sym:{name:{
                    "final_state":r["final_state"],
                    "closed_hypothetical_trades":len(r["closed_hypothetical_trades"]),
                    "frozen_exposure_reason":(r["frozen_exposure"] or {}).get("reason"),
                    "events":len(r["events"]),
                } for name,r in runs.items()}
                for sym,runs in result["by_symbol_strategy"].items()},
            "usable_for_backtesting":False,
        },ensure_ascii=False,sort_keys=True,indent=2))
        return 0
    except (VenueProbeError, ValueError, TypeError, KeyError):
        parser.exit(2,"FAIL CLOSED: KRX PIT provenance/chronology not proven\n")


if __name__=="__main__":
    raise SystemExit(main())

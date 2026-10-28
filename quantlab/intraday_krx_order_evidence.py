"""SHA-bound execution-evidence ledger for KRX research events.

A completed bar can create a *research signal*, not a broker order or fill.
A hypothetical open/close is never a receipt. This ledger carries unknown
order/position exposure through halt, gap, next session and auction. Without
authenticated broker receipts it cannot certify cash, positions or PnL.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from collections import Counter

from .intraday_krx_halt_position_stress import _read_verified
from .intraday_kis_venues import VenueProbeError

EXPECTED_STATUS="PIT_RESEARCH_PROVISIONAL_NO_CERTIFIED_FILLS_OR_PNL"
ALLOWED_EVENTS={
    "SIGNAL_AFTER_COMPLETED_BAR",
    "HYPOTHETICAL_NEXT_OPEN_ENTRY",
    "HYPOTHETICAL_CLOSE_BEFORE_AUCTION",
    "POSITION_FROZEN_AT_CB",
    "POSITION_FROZEN_MISSING_BAR",
    "AMBIGUOUS_ENTRY_HALT_OVERLAP",
    "AMBIGUOUS_ENTRY_MISSING_BAR",
}

def _advance(state: dict, event: dict) -> None:
    """Pure research event transition, never convert hypothetical into fills."""
    kind=event["event"]
    if kind not in ALLOWED_EVENTS or event.get("order_executed") is not False:
        raise VenueProbeError("Unverified source claimed real order execution")
    when={"session":event["session"],"slot":event["slot"],"event":kind}
    if state["status"]=="NO_SIGNAL":
        if kind=="SIGNAL_AFTER_COMPLETED_BAR":
            state["status"]="SIGNAL_AWAITING_EXTERNAL_ORDER_PROOF"
            state["first_signal"]=when
        elif kind=="HYPOTHETICAL_NEXT_OPEN_ENTRY":
            raise VenueProbeError("Hypothetical fill without a preceding signal")
        else:
            raise VenueProbeError("Invalid event without a preceding intent")
        return
    if state["status"] in (
        "EXPOSURE_UNCONFIRMED",
        "HALT_RECONCILIATION_REQUIRED",
        "GAP_RECONCILIATION_REQUIRED",
        "EXIT_UNCONFIRMED",
    ):
        # Once any hypothetical fill has been proposed, later daily rolls or
        # further signals cannot be treated as new, independently flat orders.
        if kind=="SIGNAL_AFTER_COMPLETED_BAR":
            state["subsequent_signals_blocked"]+=1
        elif kind=="HYPOTHETICAL_NEXT_OPEN_ENTRY":
            state["subsequent_hypothetical_entries_quarantined"]+=1
        elif kind=="HYPOTHETICAL_CLOSE_BEFORE_AUCTION":
            state["unconfirmed_exit_observations"]+=1
        elif kind in ("POSITION_FROZEN_AT_CB","AMBIGUOUS_ENTRY_HALT_OVERLAP"):
            state["status"]="HALT_RECONCILIATION_REQUIRED"
            state["halt_or_gap_after_intent"].append(when)
        elif kind in ("POSITION_FROZEN_MISSING_BAR","AMBIGUOUS_ENTRY_MISSING_BAR"):
            if state["status"]!="HALT_RECONCILIATION_REQUIRED":
                state["status"]="GAP_RECONCILIATION_REQUIRED"
            state["halt_or_gap_after_intent"].append(when)
        return
    if state["status"]=="SIGNAL_AWAITING_EXTERNAL_ORDER_PROOF":
        if kind=="SIGNAL_AFTER_COMPLETED_BAR":
            raise VenueProbeError("Duplicate first signal with no submitted intent")
        if kind=="HYPOTHETICAL_NEXT_OPEN_ENTRY":
            qty=event.get("shares")
            price=event.get("price")
            if (not isinstance(qty,int) or isinstance(qty,bool) or qty<=0 or
                not isinstance(price,(float,int)) or isinstance(price,bool) or
                price<=0):
                raise VenueProbeError("Invalid hypothetical quantity or price")
            state["status"]="EXPOSURE_UNCONFIRMED"
            state["first_hypothetical_entry"]=when
            state["unverified_scenario_shares"]=qty
            state["unverified_scenario_open_price"]=price
            # These are scenario values, not asserted broker holdings.
            state["broker_confirmed_holding_shares"]=None
            state["broker_confirmed_cash"]=None
            return
        if kind=="HYPOTHETICAL_CLOSE_BEFORE_AUCTION":
            raise VenueProbeError("Exit requires preceding hypothetical entry")
        if kind in ("POSITION_FROZEN_AT_CB","AMBIGUOUS_ENTRY_HALT_OVERLAP"):
            state["status"]="HALT_RECONCILIATION_REQUIRED"
        elif kind in ("POSITION_FROZEN_MISSING_BAR","AMBIGUOUS_ENTRY_MISSING_BAR"):
            state["status"]="GAP_RECONCILIATION_REQUIRED"
        else:
            raise VenueProbeError("Unknown signal transition")
        state["halt_or_gap_after_intent"].append(when)
        return
    raise VenueProbeError("Unknown ledger state")


def ledger_for_stream(events: list[dict], *, expected_sessions: set[str]) -> dict:
    state={
        "status":"NO_SIGNAL",
        "first_signal":None,
        "first_hypothetical_entry":None,
        "unverified_scenario_shares":None,
        "unverified_scenario_open_price":None,
        "broker_confirmed_holding_shares":None,
        "broker_confirmed_cash":None,
        "subsequent_signals_blocked":0,
        "subsequent_hypothetical_entries_quarantined":0,
        "unconfirmed_exit_observations":0,
        "halt_or_gap_after_intent":[],
        "order_sent_to_broker":False,
        "broker_receipt_verified":False,
        "realized_pnl":None,
        "certified_equity":None,
    }
    last_time=None
    for event in events:
        if (not isinstance(event,dict) or
            not isinstance(event.get("session"),str) or
            event["session"] not in expected_sessions or
            not isinstance(event.get("slot"),int) or
            isinstance(event["slot"],bool) or
            not 0<=event["slot"]<=75 or
            not isinstance(event.get("event"),str)):
            raise VenueProbeError("Invalid historical order event chronology")
        k=(event["session"],event["slot"])
        if last_time is not None and k<last_time:
            raise VenueProbeError("Out-of-order historical events")
        last_time=k
        _advance(state,event)
    state["requires_external_broker_reconciliation"]=state["status"]!="NO_SIGNAL"
    state["paper_or_live_authorized"]=False
    return state


def analyze(*, pit_dir: str|Path, output_dir: str|Path) -> dict:
    source=Path(pit_dir).expanduser().resolve()
    target=Path(output_dir).expanduser().resolve()
    if target.exists():
        raise VenueProbeError("Immutable order evidence ledger already exists")
    pit=_read_verified(source,"pit_replay.json")
    if (not isinstance(pit,dict) or pit.get("status")!=EXPECTED_STATUS or
        pit.get("schema")!="KRX_PIT_ORDERED_5M_HALT_STATE_REPLAY_V1" or
        pit.get("real_orders_executed")!=0 or
        pit.get("paper_or_live_enabled") is not False or
        pit.get("usable_for_backtesting") is not False or
        pit.get("broker_fills_verified") is not False or
        pit.get("certified_pnl_or_sharpe_calculated") is not False or
        pit.get("day_level_halt_blacklist_used_in_decision_loop") is not False):
        raise VenueProbeError("Source is not a quarantined KRX PIT result")
    sessions=pit.get("all_session_dates_preserved")
    stock_days=pit.get("population_stock_days")
    runs=pit.get("by_symbol_strategy")
    if (not isinstance(sessions,list) or not 60<=len(sessions)<=120 or
        sessions!=sorted(set(sessions)) or not isinstance(runs,dict) or
        not 1<=len(runs)<=3 or stock_days!=len(sessions)*len(runs)):
        raise VenueProbeError("Untrusted PIT population")
    from .intraday_krx_pit_replay import NAMES
    stats=Counter()
    by_symbol={}
    for symbol, strategies in runs.items():
        if (not isinstance(symbol,str) or len(symbol)!=6 or
            not symbol.isascii() or not symbol.isdigit() or
            not isinstance(strategies,dict) or set(strategies)!=set(NAMES)):
            raise VenueProbeError("Unexpected PIT strategy stream")
        by_symbol[symbol]={}
        for name in NAMES:
            stream=strategies[name]
            if (not isinstance(stream,dict) or
                stream.get("order_submissions")!=0 or
                stream.get("source_sessions")!=len(sessions) or
                stream.get("full_period_realized_return") is not None or
                not isinstance(stream.get("events"),list)):
                raise VenueProbeError("Unverified strategy or simulated order claims")
            state=ledger_for_stream(stream["events"],expected_sessions=set(sessions))
            # The prior PIT strategy's hypothetical 'flat' after OHLCV open
            # does NOT clear broker receipt uncertainty.
            if state["requires_external_broker_reconciliation"]:
                stats["unreconciled_streams"]+=1
            stats["unconfirmed_exits"]+=state["unconfirmed_exit_observations"]
            stats["quarantined_followup_entries"]+=state[
                "subsequent_hypothetical_entries_quarantined"]
            by_symbol[symbol][name]=state
    result={
        "status":"KRX_ORDER_INTENTS_REQUIRE_EXTERNAL_BROKER_RECEIPTS_NO_PNL",
        "source_pit_sha256":sha256((source/"pit_replay.json").read_bytes()).hexdigest(),
        "source_halt_5m_sha256":pit["source_summary_sha256"],
        "source_halt_stress_sha256":pit["source_stress_sha256"],
        "stock_days_preserved":stock_days,
        "stream_count":len(runs)*len(NAMES),
        "summary":dict(stats),
        "by_symbol_strategy":by_symbol,
        "no_broker_order_submissions":True,
        "hypothetical_ohlcv_open_not_broker_fill":True,
        "cancel_request_does_not_confirm_cancellation":True,
        "reported_exchange_reopen_does_not_confirm_any_fill":True,
        "unverified_exit_does_not_confirm_flat":True,
        "independent_broker_order_and_position_receipts_available":False,
        "certified_returns_or_drawdowns_computed":False,
        "usable_for_backtesting":False,
        "usable_for_paper_or_live":False,
    }
    target.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix=".krx-order-ledger-",dir=target.parent))
    try:
        data=(json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        (tmp/"order_evidence.json").write_bytes(data)
        (tmp/"order_evidence.sha256").write_text(sha256(data).hexdigest()+"\n")
        if target.exists():
            raise VenueProbeError("Concurrent immutable order evidence output")
        tmp.rename(target)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return result


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description="KRX broker-evidence gap ledger; no orders")
    parser.add_argument("--pit-dir",required=True)
    parser.add_argument("--output-dir",required=True)
    args=parser.parse_args(argv)
    try:
        value=analyze(pit_dir=args.pit_dir,output_dir=args.output_dir)
        print(json.dumps({
            "status":value["status"],
            "stock_days_preserved":value["stock_days_preserved"],
            "stream_count":value["stream_count"],
            "summary":value["summary"],
            "usable_for_backtesting":False,
        },sort_keys=True,ensure_ascii=False,indent=2))
        return 0
    except (VenueProbeError,ValueError,TypeError,KeyError):
        parser.exit(2,"FAIL CLOSED: historical order evidence missing or invalid\n")

if __name__=="__main__":
    raise SystemExit(main())

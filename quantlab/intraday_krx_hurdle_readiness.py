"""KRX 100d PIT conditional price-move hurdle and independent L2 evidence gate.

Deliberately never transfers Oct-28 KIS quotes to May-Oct-23 positions.
Counts hypothetical per-trade sign under *explicit* fee/slippage/tax stress,
never performance returns, Sharpe, trading signal promotion or verified fills.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import statistics
import tempfile

from .intraday_krx_halt_position_stress import _read_verified
from .intraday_krx_halt_scenario import INITIAL_CASH, TAX_BPS_KOSPI_2026
from .intraday_krx_pit_replay import NAMES
from .intraday_krx_dynamic_cost import _load_l2, _cost_record
from .intraday_kis_venues import VenueProbeError

FEE_BPS=3.0
BASELINE_SLIP_BPS=5.0
SLIPPAGE_GRID_BPS=(0.,5.,10.,20.,40.)
REQUIRED_UNSEEN_EVIDENCE=(
    "historical_5m_bar_time_semantics",
    "timestamp_matched_historical_L2_at_order_decisions",
    "actual_order_queue_fill_or_consistent_live_paper_execution_logs",
    "market_halt_reopen_and_unreconciled_exposure",
    "independent_unseen_walk_forward_period",
)
STATUS="KRX_CONDITIONAL_HURDLES_ONLY_NO_EXECUTION_CERTIFICATION"


def _assert_pit(pit:dict)->list[str]:
    sessions=pit.get("all_session_dates_preserved")
    if (pit.get("status")!="PIT_RESEARCH_PROVISIONAL_NO_CERTIFIED_FILLS_OR_PNL" or
        pit.get("schema")!="KRX_PIT_ORDERED_5M_HALT_STATE_REPLAY_V1" or
        not isinstance(sessions,list) or len(sessions)!=100 or
        sessions!=sorted(set(sessions)) or
        any(not isinstance(d,str) or len(d)!=10 for d in sessions) or
        pit.get("population_stock_days")!=200 or
        pit.get("all_orders_hypothetical") is not True or
        pit.get("real_orders_executed")!=0 or
        pit.get("broker_fills_verified") is not False or
        pit.get("vendor_timestamp_contract_verified") is not False or
        pit.get("certified_pnl_or_sharpe_calculated") is not False or
        pit.get("paper_or_live_enabled") is not False or
        pit.get("training_validation_test_windows_are_not_blind_after_prior_exposure") is not True):
        raise VenueProbeError("PIT report cannot certify KRX historical fills or strategy")
    for d in sessions:
        try:date.fromisoformat(d)
        except ValueError as exc:raise VenueProbeError("Bad PIT session date") from exc
    source=pit.get("by_symbol_strategy")
    if (not isinstance(source,dict) or set(source)!={"005930","000660"} or
        any(not isinstance(v,dict) or set(v)!=set(NAMES) for v in source.values())):
        raise VenueProbeError("Incomplete eight-strategy PIT source")
    return sessions


def _read_market_reference(path:Path|str,last_session:str,
                           pit_sha256:str,l2_root:Path|str)->dict:
    source=Path(path).expanduser().resolve()
    report=_read_verified(source,"cost_sensitivity.json")
    rows=report.get("per_snapshot")
    if (report.get("status")!="KRX_OBSERVED_L2_PRICE_ONLY_COST_SENSITIVITY_NOT_FILLS" or
        report.get("actual_broker_fill_records")!=0 or
        report.get("strategy_returns") is not None or
        report.get("eligible_for_live_or_paper_trading") is not False or
        report.get("historical_backtest_returns_computed") is not False or
        report.get("same_snapshot_roundtrip_not_executable_trade_pair") is not True or
        report.get("historical_pit_last_session")!=last_session or
        report.get("historical_pit_replay_sha256")!=pit_sha256 or
        not isinstance(rows,list) or not rows or len(rows)>100):
        raise VenueProbeError("Unknown L2 market evidence reference")
    group=set(report.get("by_sample_group",{}))
    if len(group)!=1 or group!={"2026-10-28_L2"}:
        raise VenueProbeError("Only independently observed 2026-10-28 reference accepted")
    symbols=Counter()
    days=set()
    for row in rows:
        sym=row.get("symbol")
        if sym not in ("005930","000660") or row.get("sample_group")!="2026-10-28_L2":
            raise VenueProbeError("Unexpected stock/market quote reference")
        symbols[sym]+=1
        instant=row.get("quote_kst")
        if not isinstance(instant,str) or instant[:10]!="2026-10-28":
            raise VenueProbeError("Wrong L2 reference market day")
        days.add(instant[:10])
        if row.get("queue_position_and_cancellations_not_observed") is not True:
            raise VenueProbeError("L2 quote cannot be treated as a fill")
    if not symbols["005930"] or not symbols["000660"]:
        raise VenueProbeError("Incomplete market reference population")
    independently_reparsed,provenance=_load_l2(Path(l2_root).expanduser().resolve())
    if (report.get("sources")!=[provenance] or
        report["snapshot_count"]!=len(rows) or
        [_cost_record(item) for item in independently_reparsed]!=rows):
        raise VenueProbeError("L2 cost reference does not reconcile with real raw source")
    if any(day<=last_session for day in days):
        raise VenueProbeError("L2 quote date overlaps historical PIT")
    return {
        "sha256":sha256((source/"cost_sensitivity.json").read_bytes()).hexdigest(),
        "quoted_sessions":sorted(days),
        "observed_quotes_by_symbol":dict(sorted(symbols.items())),
        "historical_100d_order_matching_quote_count":0,
        "can_calibrate_historical_slippage":False,
        "can_verify_historical_fill":False,
        "price_reference":"midpoint_not_historical_5m_open",
    }


def _classify(sessions:list[str],session:str)->str:
    try:idx=sessions.index(session)
    except ValueError as exc:raise VenueProbeError("Trade outside SHA-verified PIT period") from exc
    return "train" if idx<60 else "validation" if idx<80 else "test"


def _entry_hurdle(entry:float,exit_:float,fee:float,tax:float)->float:
    """Max same-side slippage (bp) for a zero-net *per share* hypothetical fill."""
    return ((exit_*(1-(fee+tax)/10000)-entry*(1+fee/10000))
            *10000/(entry+exit_))


def _trade_row(trade:dict,symbol:str,strategy:str,sessions:list[str])->dict:
    if (not isinstance(trade,dict) or trade.get("actual_fill") is not False or
        trade.get("symbol")!=symbol or trade.get("strategy")!=strategy):
        raise VenueProbeError("Untrusted hypothetical trade identity")
    entry=trade.get("entry")
    if not isinstance(entry,dict) or entry.get("session")!=trade.get("session"):
        raise VenueProbeError("Unmatched entry and hypothetical close session")
    e=entry.get("entry_price_assumed")
    x=trade.get("hypothetical_exit_price")
    qty=entry.get("shares_assumed")
    if (any(type(v) not in (float,int) or not math.isfinite(v) or v<=0
            for v in (e,x,qty)) or
        type(qty)!=int or
        entry.get("fee_slip_bps_each_side")!=FEE_BPS+BASELINE_SLIP_BPS or
        not isinstance(trade.get("hypothetical_exit_slot_start"),str) or
        trade["hypothetical_exit_slot_start"][:10]!=trade["session"] or
        trade.get("conditional_cash_return") is None):
        raise VenueProbeError("Untrusted conditional price or quantity")
    # Independently cross-check the existing 5bp/3bp/20bp example.
    estimated=(qty*(x*(1-(FEE_BPS+BASELINE_SLIP_BPS+TAX_BPS_KOSPI_2026)/10000)
                    -e*(1+(FEE_BPS+BASELINE_SLIP_BPS)/10000))/INITIAL_CASH)
    if not math.isclose(trade["conditional_cash_return"],estimated,rel_tol=1e-9,abs_tol=1e-11):
        raise VenueProbeError("Original conditional cash return cannot be reconciled")
    t=_classify(sessions,trade["session"])
    hurdle=_entry_hurdle(e,x,FEE_BPS,TAX_BPS_KOSPI_2026)
    survives={}
    for slip in SLIPPAGE_GRID_BPS:
        buy=e*(1+(FEE_BPS+slip)/10000)
        sell=x*(1-(FEE_BPS+slip+TAX_BPS_KOSPI_2026)/10000)
        survives[str(int(slip))]=bool(sell>buy)
    return {
        "session":trade["session"],"split":t,
        "assumed_entry_price":e,"assumed_exit_price":x,
        "assumed_shares":qty,
        "gross_price_move_bps":(x/e-1)*10000,
        "max_side_slippage_for_zero_hypothetical_net_bps":hurdle,
        "positive_conditional_cash_after_slippage_grid":survives,
        "real_broker_fill":False,
        "price_timestamps_and_actual_executable_trades_certified":False,
    }


def analyze(*,pit_dir:Path|str,cost_dir:Path|str,l2_root:Path|str,
            output_dir:Path|str)->dict:
    target=Path(output_dir).expanduser().resolve()
    if target.exists():raise VenueProbeError("Immutable strategy hurdle report exists")
    root=Path(pit_dir).expanduser().resolve()
    pit=_read_verified(root,"pit_replay.json")
    sessions=_assert_pit(pit)
    pit_sha256=sha256((root/"pit_replay.json").read_bytes()).hexdigest()
    reference=_read_market_reference(cost_dir,sessions[-1],pit_sha256,l2_root)
    results={}
    n=0
    for symbol,strategies in sorted(pit["by_symbol_strategy"].items()):
        results[symbol]={}
        for strategy,data in sorted(strategies.items()):
            if data.get("strategy")!=strategy or data.get("order_submissions")!=0:
                raise VenueProbeError("Strategy metadata cannot be treated as orders")
            state=data.get("final_state")
            frozen=data.get("frozen_exposure")
            if ((state=="FLAT" and frozen is not None) or
                (state=="FROZEN_UNRECONCILED" and
                 (not isinstance(frozen,dict) or frozen.get("realized_pnl") is not None)) or
                state not in ("FLAT","FROZEN_UNRECONCILED")):
                raise VenueProbeError("Unknown PIT risk and position state")
            source=data.get("closed_hypothetical_trades")
            if not isinstance(source,list) or len(source)>100:
                raise VenueProbeError("Unbounded closed trade population")
            rows=[_trade_row(t,symbol,strategy,sessions) for t in source]
            if len(set(r["session"] for r in rows))!=len(rows):
                raise VenueProbeError("Duplicate hypothetical close")
            by_split=Counter(row["split"] for row in rows)
            if any(data.get("window_counts",{}).get(split,{}).get("hypothetical_closed",0)!=by_split[split]
                   for split in ("train","validation","test")):
                raise VenueProbeError("PIT window counts disagree with conditional trades")
            n+=len(rows)
            grid={str(int(slip)):sum(row["positive_conditional_cash_after_slippage_grid"][str(int(slip))]
                                     for row in rows)
                  for slip in SLIPPAGE_GRID_BPS}
            results[symbol][strategy]={
                "final_position_state":state,
                "unknown_unreconciled_exposure":state=="FROZEN_UNRECONCILED",
                "closed_conditional_trade_count":len(rows),
                "closed_conditional_trade_counts_by_window":{
                    label:by_split[label] for label in ("train","validation","test")},
                "conditional_positive_cash_count_if_all_assumed_fills":grid,
                "conditional_max_slippage_bps_median":(
                    statistics.median(r["max_side_slippage_for_zero_hypothetical_net_bps"]
                                      for r in rows) if rows else None),
                "historical_order_specific_L2_matches":0,
                "strategy_total_return":None,"sharpe":None,"max_drawdown":None,
                "eligible_for_paper_or_live":False,
                "hypothetical_trades":rows,
            }
    report={
        "status":STATUS,
        "source_pit_sha256":pit_sha256,
        "market_reference":reference,
        "historical_window_sessions":{"train":60,"validation":20,"test":20},
        "previously_exposed_holdout_not_blind":True,
        "real_order_and_fill_count":0,
        "hypothetical_closed_trade_instances_across_strategies":n,
        "eight_symbol_strategy_accounts":8,
        "hurdle_grid_side_slippage_bps":list(SLIPPAGE_GRID_BPS),
        "independent_fee_each_side_bps":FEE_BPS,
        "independent_sale_tax_bps":TAX_BPS_KOSPI_2026,
        "baseline_conditional_slippage_bps":BASELINE_SLIP_BPS,
        "baseline_trades_were_not_verified_fills":True,
        "unresolved_execution_requirements":list(REQUIRED_UNSEEN_EVIDENCE),
        "historical_returns_recomputed_with_Oct28_L2":False,
        "historical_strategy_profit_or_sharpe_certified":False,
        "strategy_results":results,
    }
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".krx-hurdles-",dir=target.parent))
    try:
        payload=(json.dumps(report,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        (stage/"hurdle_readiness.json").write_bytes(payload)
        (stage/"hurdle_readiness.sha256").write_text(sha256(payload).hexdigest()+"\n")
        if target.exists():raise VenueProbeError("Concurrent immutable hurdle report")
        stage.rename(target)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return report


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="KRX hypothetical hurdle gate; NO certified fills")
    p.add_argument("--pit-dir",required=True)
    p.add_argument("--cost-dir",required=True)
    p.add_argument("--l2-root",required=True)
    p.add_argument("--output-dir",required=True)
    a=p.parse_args(argv)
    try:
        d=analyze(pit_dir=a.pit_dir,cost_dir=a.cost_dir,
                  l2_root=a.l2_root,output_dir=a.output_dir)
        print(json.dumps({
            "status":d["status"],"hypothetical_closed_trade_instances":
                d["hypothetical_closed_trade_instances_across_strategies"],
            "by_symbol_strategy":{
                sy:{st:{"closed":v["closed_conditional_trade_count"],
                        "unreconciled":v["unknown_unreconciled_exposure"],
                        "test_closed":v["closed_conditional_trade_counts_by_window"]["test"],
                        "positive_with_hypothetical_5bp":v[
                            "conditional_positive_cash_count_if_all_assumed_fills"]["5"]}
                    for st,v in data.items()}
                for sy,data in d["strategy_results"].items()},
            "certified_strategy_returns":None,"real_orders":0,
        },ensure_ascii=False,sort_keys=True,indent=2))
        return 0
    except (VenueProbeError,ValueError,KeyError,TypeError,OSError):
        print(json.dumps({"status":"FAIL_CLOSED_NO_TRADING_SIGNAL_OR_EXECUTION_PROOF",
                          "real_orders":0,"certified_strategy_returns":None}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

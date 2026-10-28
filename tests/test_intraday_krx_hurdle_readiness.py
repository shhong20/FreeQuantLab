"""Hypothetical KRX price-move hurdles never certify executable returns."""
from __future__ import annotations

from datetime import date, timedelta
from hashlib import sha256
import json

import pytest

from quantlab.intraday_krx_dynamic_cost import analyze as analyze_cost
from quantlab.intraday_krx_hurdle_readiness import (
    analyze, _entry_hurdle, _trade_row, _assert_pit,
)
from quantlab.intraday_krx_pit_replay import NAMES
from quantlab.intraday_kis_venues import VenueProbeError
from tests.test_intraday_krx_dynamic_cost import make_l2, _save


def fixture(tmp_path):
    l2=make_l2(tmp_path)
    pit=tmp_path/"pit"
    pit.mkdir()
    sessions=[(date(2026,5,15)+timedelta(days=i)).isoformat()
              for i in range(100)]
    entry=100000.0
    close=102000.0
    qty=10
    baseline=qty*(close*(1-(3+5+20)/10000)-entry*(1+(3+5)/10000))/10_000_000
    trade={
        "session":sessions[0],"symbol":"005930","strategy":"orb_5m_rvol1.5",
        "actual_fill":False,"conditional_cash_return":baseline,
        "entry":{"session":sessions[0],"entry_price_assumed":entry,
                 "shares_assumed":qty,"fee_slip_bps_each_side":8.0},
        "hypothetical_exit_price":close,
        "hypothetical_exit_slot_start":sessions[0]+"T15:15:00+09:00",
    }
    by_symbol={}
    for sy in ("005930","000660"):
        by_symbol[sy]={}
        for strategy in NAMES:
            frozen=sy=="000660" and strategy=="intraday_momentum_30m"
            used=[trade] if sy=="005930" and strategy=="orb_5m_rvol1.5" else []
            by_symbol[sy][strategy]={
                "strategy":strategy,"order_submissions":0,
                "final_state":"FROZEN_UNRECONCILED" if frozen else "FLAT",
                "frozen_exposure":{"realized_pnl":None,"reason":"CB"} if frozen else None,
                "closed_hypothetical_trades":used,
                "window_counts":{
                    k:({"hypothetical_closed":len(used)} if k=="train" and used else {})
                    for k in ("train","validation","test")},
            }
    obj={
        "status":"PIT_RESEARCH_PROVISIONAL_NO_CERTIFIED_FILLS_OR_PNL",
        "schema":"KRX_PIT_ORDERED_5M_HALT_STATE_REPLAY_V1",
        "all_session_dates_preserved":sessions,
        "population_stock_days":200,
        "all_orders_hypothetical":True,
        "real_orders_executed":0,"broker_fills_verified":False,
        "vendor_timestamp_contract_verified":False,
        "certified_pnl_or_sharpe_calculated":False,
        "paper_or_live_enabled":False,
        "training_validation_test_windows_are_not_blind_after_prior_exposure":True,
        "by_symbol_strategy":by_symbol,
    }
    _save(pit,"pit_replay",obj)
    cost_dir=tmp_path/"cost"
    analyze_cost(output_dir=cost_dir,l2_roots=[l2],pit_dir=pit)
    return (pit,cost_dir,l2,obj)


def test_real_sha_sources_and_hypothetical_hurdles_stay_separate(tmp_path):
    pit,cost,l2,_=fixture(tmp_path)
    result=analyze(pit_dir=pit,cost_dir=cost,l2_root=l2,
                   output_dir=tmp_path/"out")
    assert result["hypothetical_closed_trade_instances_across_strategies"]==1
    assert result["eight_symbol_strategy_accounts"]==8
    assert result["historical_returns_recomputed_with_Oct28_L2"] is False
    assert result["historical_strategy_profit_or_sharpe_certified"] is False
    assert result["market_reference"][
        "historical_100d_order_matching_quote_count"]==0
    assert result["market_reference"]["observed_quotes_by_symbol"]=={
        "000660":1,"005930":1}
    row=result["strategy_results"]["005930"]["orb_5m_rvol1.5"]
    assert row["closed_conditional_trade_count"]==1
    assert row["closed_conditional_trade_counts_by_window"]=={
        "train":1,"validation":0,"test":0}
    assert row["conditional_positive_cash_count_if_all_assumed_fills"]["5"]==1
    assert row["strategy_total_return"] is None
    assert row["hypothetical_trades"][0]["real_broker_fill"] is False
    frozen=result["strategy_results"]["000660"]["intraday_momentum_30m"]
    assert frozen["final_position_state"]=="FROZEN_UNRECONCILED"
    assert frozen["unknown_unreconciled_exposure"] is True
    assert frozen["sharpe"] is None
    f=tmp_path/"out"/"hurdle_readiness.json"
    assert sha256(f.read_bytes()).hexdigest()==(
        tmp_path/"out"/"hurdle_readiness.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="exists"):
        analyze(pit_dir=pit,cost_dir=cost,l2_root=l2,output_dir=tmp_path/"out")


def test_max_hurdle_is_monotone_in_gross_price_move():
    assert _entry_hurdle(100000,102000,3,20)>_entry_hurdle(
        100000,100000,3,20)
    assert _entry_hurdle(100000,100000,3,20)<0
    assert _entry_hurdle(100000,102000,3,20)>40


def test_tampered_cost_sha_rehashed_still_fails_independent_raw_l2_check(tmp_path):
    pit,cost,l2,_=fixture(tmp_path)
    report=json.loads((cost/"cost_sensitivity.json").read_text())
    report["per_snapshot"][0]["best_ask"]+=1000
    _save(cost,"cost_sensitivity",report)
    with pytest.raises(VenueProbeError,match="does not reconcile"):
        analyze(pit_dir=pit,cost_dir=cost,l2_root=l2,
                output_dir=tmp_path/"out")
    assert not (tmp_path/"out").exists()


def test_conditional_trade_tampering_rejected(tmp_path):
    pit,cost,l2,original=fixture(tmp_path)
    original["by_symbol_strategy"]["005930"]["orb_5m_rvol1.5"][
        "closed_hypothetical_trades"][0]["conditional_cash_return"]=0.99
    _save(pit,"pit_replay",original)
    # The first independent failure is the cost report's PIT SHA mismatch;
    # even if that report were also forged, the individual cash example check
    # rejects a manipulated conditional trade.
    with pytest.raises(VenueProbeError,match="Unknown L2 market evidence"):
        analyze(pit_dir=pit,cost_dir=cost,l2_root=l2,
                output_dir=tmp_path/"out")
    bad=original["by_symbol_strategy"]["005930"]["orb_5m_rvol1.5"][
        "closed_hypothetical_trades"][0]
    with pytest.raises(VenueProbeError,match="cannot be reconciled"):
        _trade_row(bad,"005930","orb_5m_rvol1.5",
                   original["all_session_dates_preserved"])


def test_frozen_or_certified_pnl_report_never_accepted(tmp_path):
    pit,cost,l2,data=fixture(tmp_path)
    data["broker_fills_verified"]=True
    _save(pit,"pit_replay",data)
    with pytest.raises(VenueProbeError,match="cannot certify"):
        analyze(pit_dir=pit,cost_dir=cost,l2_root=l2,
                output_dir=tmp_path/"out")


def test_missing_order_aligned_quote_is_not_a_fill_proof(tmp_path):
    pit,cost,l2,_=fixture(tmp_path)
    report=json.loads((cost/"cost_sensitivity.json").read_text())
    report["per_snapshot"][0]["quote_kst"]="2026-05-20T13:20:00+09:00"
    _save(cost,"cost_sensitivity",report)
    with pytest.raises(VenueProbeError,match="Wrong L2 reference"):
        analyze(pit_dir=pit,cost_dir=cost,l2_root=l2,
                output_dir=tmp_path/"out")

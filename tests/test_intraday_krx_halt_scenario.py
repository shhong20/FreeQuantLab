"""Regression for read-only, conditional 5m fill scenario; no real brokerage fills."""
from __future__ import annotations
from datetime import date,timedelta
from hashlib import sha256
import json
import math
import pytest

import quantlab.intraday_krx_halt_5m as source_mod
from quantlab.intraday_krx_halt_position_stress import replay
from quantlab.intraday_krx_halt_scenario import (
    evaluate, _simulate_day, _metrics, _signed_cost_return, _prehalt_momentum_exposure, STRATEGIES,
)
from quantlab.intraday_krx_5m_preview import CLOCKS
from quantlab.intraday_kis_venues import VenueProbeError


def _bars(*, closing=106, opening=100):
    rows=[]
    for i in range(76):
        price=opening if i<5 else 103 if i<75 else closing
        rows.append({"open":price,"high":price+1,"low":price-1,
                     "close":price,"volume":10000})
    return rows


def _full(tmp_path,monkeypatch):
    sessions=[]
    d=date(2025,9,1)
    while len(sessions)<65:
        if d.weekday()<5:
            sessions.append(d.isoformat())
        d+=timedelta(days=1)
    root=tmp_path/"source"
    observations={}
    for k,day in enumerate(sessions):
        (root/f"KIS_J_EXT_005930_{day}").mkdir(parents=True)
        rows={}
        for j,clock in enumerate(CLOCKS):
            if k==4 and j==1:
                continue  # one safe-calendar day with missing vendor data
            price=100 if j<29 else 103 if j<375 else 106
            rows[clock]={
                "stck_bsop_date":day.replace("-",""),
                "stck_cntg_hour":clock,"stck_oprc":str(price),
                "stck_hgpr":str(price+1), "stck_lwpr":str(price-1),
                "stck_prpr":str(price), "cntg_vol":"10000",
            }
        rows["153000"]={
            "stck_bsop_date":day.replace("-",""),
            "stck_cntg_hour":"153000","stck_oprc":"106",
            "stck_hgpr":"106","stck_lwpr":"106",
            "stck_prpr":"106","cntg_vol":"200",
        }
        observations[day]=rows
    def read(source,symbol,session,market):
        assert source==root and symbol=="005930" and market=="J"
        return {"source_minute_bar_sha256":"a"*64},observations[session]
    monkeypatch.setattr(source_mod,"_read_one",read)
    out=tmp_path/"halt"
    source_mod.build(source_root=root,out_dir=out,
                     symbols=("005930",),sessions=tuple(sessions))
    stress=tmp_path/"stress"
    replay(input_root=out,out_dir=stress)
    return out,stress,sessions


def test_future_entry_or_exit_candle_volume_never_sizes_order():
    base=_bars()
    hist={1:[1000]*11,3:[3000]*11,6:[6000]*11}
    # Momentum at first 30m closing -> hypothetical next bar open.
    result=_simulate_day(symbol="005930",session="2025-09-30",
        bars=base,history={k:list(v) for k,v in hist.items()},
        fee=3,slip=5,tax=20)
    t=next(x for x in result if x["strategy"]=="intraday_momentum_30m")
    assert t["hypothetical_entry_slot_start_kst"].endswith("09:30:00+09:00")
    assert t["hypothetical_exit_slot_start_kst"].endswith("15:15:00+09:00")
    assert t["fill_observed"] is False and t["order_submitted"] is False
    assert t["assumed_shares"]>0
    altered=_bars()
    altered[6]["volume"]=0
    altered[75]["volume"]=0
    new=_simulate_day(symbol="005930",session="2025-09-30",
        bars=altered,history={k:list(v) for k,v in hist.items()},
        fee=3,slip=5,tax=20)
    u=next(x for x in new if x["strategy"]=="intraday_momentum_30m")
    assert t["assumed_shares"]==u["assumed_shares"]
    assert t["hypothetical_net_return_base"]==u["hypothetical_net_return_base"]


def test_costs_reduce_hypothetical_return_and_flat_days_in_denominator():
    positive=_signed_cost_return(100,105,100,fee=3,slip=5,tax=20)
    expensive=_signed_cost_return(100,105,100,fee=6,slip=10,tax=20)
    assert positive>expensive
    assert _signed_cost_return(100,100,100,fee=3,slip=5,tax=20)<0
    days=["2025-09-01","2025-09-02","2025-09-03"]
    result=_metrics(days,[{"session":days[0],
                          "hypothetical_net_return_base":positive}],
                    "hypothetical_net_return_base")
    assert result["population_days_including_blocked"]==3
    assert result["hypothetical_trades"]==1
    assert result["hypothetical_compounded_return"]==pytest.approx(positive)
    assert result["certified_strategy_performance"] is False


def test_real_fixture_e2e_risk_coupling_and_no_unsafe_date_pnl(tmp_path,monkeypatch):
    root,risk,days=_full(tmp_path,monkeypatch)
    result=evaluate(input_root=root,stress_root=risk,out_dir=tmp_path/"scenario")
    assert result["status"]=="CONDITIONAL_KRX_OPEN_FILL_SCENARIO_NOT_VALIDATED_BACKTEST"
    assert result["all_stock_days_preserved"]==65
    assert len(result["risk_day_rows"])==65
    assert result["risk_day_rows"][4]["assumed_entries_allowed"] is False
    assert result["risk_day_rows"][4]["realized_pnl"] is None
    assert result["day_dispositions"]["ENTIRE_DAY_STRATEGY_PNL_BLOCKED_UNKNOWN_GAPS"]==1
    assert result["precommitted_windows"]["untouched_test"][2]==13
    assert result["usable_for_backtesting"] is False
    assert result["usable_for_paper_or_live"] is False
    sym=result["by_symbol"]["005930"]
    assert sym["blocked_stock_days"]==1
    assert sym["chosen_from_validation_only"]["intraday_momentum"]==STRATEGIES[-1]
    out=sym["untouched_test_selected_only"]["intraday_momentum"]
    assert out["assumed_cost_scenario"]["hypothetical_trades"]==13
    assert out["status"]=="CONDITIONAL_SCENARIO_MINIMUM_TRADES_MET"
    assert out["assumed_cost_scenario"]["hypothetical_compounded_return"] > (
        out["double_fee_slippage_scenario"]["hypothetical_compounded_return"])
    assert result["real_orders_executed"]==0
    assert result["whole_day_risk_gate_is_oracular_not_live_implementable"] is True
    assert result["holdout_untouched_in_full_information_sense"] is False
    p=tmp_path/"scenario"/"scenario_report.json"
    assert sha256(p.read_bytes()).hexdigest()==(
        tmp_path/"scenario"/"scenario_report.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already exists"):
        evaluate(input_root=root,stress_root=risk,out_dir=tmp_path/"scenario")


def test_adversarial_stress_tampering_blocks_no_fill_violation(tmp_path,monkeypatch):
    root,risk,days=_full(tmp_path,monkeypatch)
    p=risk/"stress_report.json"
    data=json.loads(p.read_bytes())
    data["positions_never_automatically_liquidated_at_missing_bar"]=False
    blob=(json.dumps(data,sort_keys=True)+"\n").encode()
    p.write_bytes(blob)
    (risk/"stress_report.sha256").write_text(sha256(blob).hexdigest()+"\n")
    with pytest.raises(VenueProbeError,match="no-fill"):
        evaluate(input_root=root,stress_root=risk,out_dir=tmp_path/"scenario")
    assert not (tmp_path/"scenario").exists()


@pytest.mark.parametrize("cost",[-1,float("nan"),float("inf"),True,100000])
def test_invalid_fees_are_rejected_before_writing(tmp_path,cost):
    with pytest.raises(VenueProbeError,match="cost"):
        evaluate(input_root=tmp_path,stress_root=tmp_path,
                 out_dir=tmp_path/"scenario",fee_bps=cost)
    assert not (tmp_path/"scenario").exists()

def test_momentum_could_have_entered_before_halt_without_future_event_oracle():
    day="2026-07-28"
    slots=[]
    for i in range(76):
        v=100 if i==0 else 103 if i>=5 else 100
        slots.append({
            "timestamp":f"{day}T{(540+5*i)//60:02d}:{(540+5*i)%60:02d}:00+09:00",
            "ohlcv":{"open":v,"high":v+1,"low":v-1,"close":v,"volume":10000},
            "source_observed_count":5,
            "market_reported_cb_window_overlap":False,
        })
    row={"reported_cb":{"activation_kst":f"{day}T10:13:43+09:00"},
         "slots":slots}
    x=_prehalt_momentum_exposure(row)
    assert x["status"]=="POSSIBLE_PRE_HALT_LONG_EXPOSURE_UNPRICED"
    assert x["potential_position_at_halt"] is True
    assert x["realized_pnl"] is None
    assert x["signal_complete_at_kst"].endswith("09:30:00+09:00")
    # A halt before entry makes a future day's early momentum unusable.
    row["reported_cb"]["activation_kst"]=f"{day}T09:03:43+09:00"
    early=_prehalt_momentum_exposure(row)
    assert early["potential_position_at_halt"] is False
    # Incomplete first-30m bar must never invent a prehalt entry.
    row["slots"][4]["ohlcv"]=None
    assert _prehalt_momentum_exposure(row)["potential_position_at_halt"] is None

"""PIT KRX replay: event chronology, conservative holds, no oracle ban."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
import pytest

from quantlab.intraday_krx_pit_replay import (
    _available, _candidate_signal, _costs, _replay_one, replay,
)
from quantlab.intraday_kis_venues import VenueProbeError
import quantlab.intraday_krx_halt_5m as halt_mod
from quantlab.intraday_krx_halt_position_stress import replay as stress_replay


def day(session="2026-07-07", *, halt="13:51:34", gaps=()):
    rows=[]
    for i in range(76):
        clock=9*60+i*5
        label=f"{clock//60:02d}:{clock%60:02d}"
        price=100 if i<5 else 103
        # Makes 30m momentum known at 09:30, next-open entry at index 6.
        complete=i not in gaps
        rows.append({
            "timestamp":f"{session}T{label}:00+09:00",
            "ohlcv":{"open":price,"high":price+1,
                     "low":price-1,"close":price,
                     "volume":10000} if complete else None,
            "source_observed_count":5 if complete else 0,
            "state":("FULL_OBSERVED_5M_PROVISIONAL" if complete else
                     "INCOMPLETE_SOURCE_GAP_UNEXPLAINED"),
            "market_reported_cb_window_overlap":False,
        })
    event=None
    if halt is not None:
        from datetime import datetime, timedelta
        begin=datetime.fromisoformat(f"{session}T{halt}+09:00")
        end=begin+timedelta(minutes=30)
        event={"activation_kst":begin.isoformat(),
               "conservative_continuous_order_blackout_until_kst":end.isoformat(),
               "source_quality":"PUBLIC_REPORT_NOT_EXCHANGE_EXECUTION_EVENT_FEED"}
        for row in rows:
            st=datetime.fromisoformat(row["timestamp"])
            overlaps=st < end and st+timedelta(minutes=5)>begin
            if overlaps:
                row["market_reported_cb_window_overlap"]=True
                row["ohlcv"]=None
                row["source_observed_count"]=0
                row["state"]="INCOMPLETE_CB_CORRELATED_UNVERIFIED"
    return {"symbol":"005930","session":session,"reported_cb":event,
            "day_disposition":(
                "ENTIRE_DAY_STRATEGY_PNL_BLOCKED_MARKET_HALT_RISK" if halt
                else "PROVISIONAL_COMPLETE_DAY_VENDOR_SEMANTICS_HOLD"
            ),"slots":rows}


def run(days, name="intraday_momentum_30m"):
    return _replay_one(name,days,fee=3.,slip=5.,tax=20.,
                       windows={x["session"]:"test" for x in days})


def test_future_reported_halt_does_not_block_morning_entry():
    with_event=day()
    without=day(halt=None)
    a=run([with_event]);b=run([without])
    first_a=[x for x in a["events"] if x["event"]=="HYPOTHETICAL_NEXT_OPEN_ENTRY"]
    first_b=[x for x in b["events"] if x["event"]=="HYPOTHETICAL_NEXT_OPEN_ENTRY"]
    assert first_a and first_b
    assert first_a[0]==first_b[0]
    assert first_a[0]["slot"]==6
    assert a["final_state"]=="FROZEN_UNRECONCILED"
    assert a["frozen_exposure"]["reason"]=="POSITION_OVERLAPS_REPORTED_CB"
    assert a["closed_hypothetical_trades"]==[]
    assert b["final_state"]=="FLAT"
    assert len(b["closed_hypothetical_trades"])==1
    assert all(x["order_executed"] is False for x in a["events"])


def test_gap_after_entry_carries_held_position_into_next_day_without_fills():
    first=day(session="2026-09-01",halt=None,gaps=(30,))
    next_day=day(session="2026-09-02",halt=None)
    result=run([first,next_day])
    assert result["final_state"]=="FROZEN_UNRECONCILED"
    assert result["frozen_exposure"]["session"]=="2026-09-01"
    assert result["frozen_exposure"]["reason"]=="POSITION_AT_UNKNOWN_SOURCE_GAP"
    assert result["closed_hypothetical_trades"]==[]
    assert len([x for x in result["events"]
                if x["event"]=="HYPOTHETICAL_NEXT_OPEN_ENTRY"])==1
    assert result["window_counts"]["test"]["frozen_at_day_end"]==2
    assert result["full_period_realized_return"] is None
    assert result["full_period_mdd"] is None


def test_next_bar_missing_makes_pending_entry_ambiguous_not_cash():
    result=run([day(halt=None,gaps=(6,))])
    assert result["final_state"]=="FROZEN_UNRECONCILED"
    assert result["frozen_exposure"]["reason"]=="PENDING_ENTRY_ON_MISSING_BAR"
    assert result["frozen_exposure"]["entry"] is None
    assert result["closed_hypothetical_trades"]==[]
    assert not any(x["event"]=="HYPOTHETICAL_NEXT_OPEN_ENTRY"
                   for x in result["events"])


def test_early_halt_blocks_only_after_activation_not_prior_sessions():
    d0=day(session="2026-06-05",halt=None)
    d1=day(session="2026-06-08",halt="09:03:42")
    result=run([d0,d1])
    assert len(result["closed_hypothetical_trades"])==1
    assert result["closed_hypothetical_trades"][0]["session"]=="2026-06-05"
    assert result["final_state"]=="FLAT"
    assert result["window_counts"]["test"]["cb_day_observed_ex_post"]==1
    assert not any(x["session"]=="2026-06-08" and
                   x["event"]=="HYPOTHETICAL_NEXT_OPEN_ENTRY"
                   for x in result["events"])


def test_even_if_missing_day_disposition_lies_engine_uses_slot_clock_not_label():
    d=day(halt=None,gaps=(30,))
    d["day_disposition"]="PROVISIONAL_COMPLETE_DAY_VENDOR_SEMANTICS_HOLD"
    result=run([d])
    assert result["final_state"]=="FROZEN_UNRECONCILED"
    assert result["frozen_exposure"]["reason"]=="POSITION_AT_UNKNOWN_SOURCE_GAP"


def test_full_5m_bar_and_initial_fee_constraints():
    assert _available(day(halt=None)["slots"][3])
    assert not _available(day(halt=None,gaps=(3,))["slots"][3])
    with pytest.raises(VenueProbeError,match="cost"):
        _costs(-1,5,20)
    with pytest.raises(VenueProbeError,match="cost"):
        _costs(3,float("nan"),20)


def test_tampered_pinned_stress_blocks_e2e(tmp_path):
    raw=tmp_path/"halt"
    raw.mkdir()
    dates=[]
    current=date(2026,5,4)
    while len(dates)<60:
        if current.weekday()<5:
            dates.append(current.isoformat())
        current+=timedelta(days=1)
    blob=(json.dumps({
        "status":"HALT_AWARE_5M_CANDIDATES_RESEARCH_ONLY_NO_PNL",
        "usable_for_backtesting":False,"usable_for_paper_or_live":False,
        "sessions":dates,
        "day_records_without_slots":[],
        "symbols":["005930"],"total_stock_days":60,
        "total_5m_clock_slots":4560,
    },sort_keys=True)+"\n").encode()
    (raw/"halt_5m_summary.json").write_bytes(blob)
    (raw/"halt_5m_summary.sha256").write_text(sha256(blob).hexdigest()+"\n")
    stress=tmp_path/"stress"
    stress.mkdir()
    fake={"status":"HYPOTHETICAL_HALT_ORDER_POLICY_AND_GAP_OBSERVATION_NO_PNL",
          "source_summary_sha256":sha256(blob).hexdigest(),
          "source_stock_days":60,"usable_for_backtesting":False,
          "usable_for_paper_or_live":False,
          "positions_never_automatically_liquidated_at_missing_bar":False,
          "unfilled_orders_never_assumed_executed_on_reopening":True,
          "halt_exposure_scenarios":[],"scenario_count":0}
    body=(json.dumps(fake,sort_keys=True)+"\n").encode()
    (stress/"stress_report.json").write_bytes(body)
    (stress/"stress_report.sha256").write_text(sha256(body).hexdigest()+"\n")
    with pytest.raises(VenueProbeError,match="stress mismatch"):
        replay(source_root=raw,stress_root=stress,output_dir=tmp_path/"out")
    assert not (tmp_path/"out").exists()

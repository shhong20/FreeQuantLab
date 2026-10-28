"""Execution evidence: ambiguous fills, halts, cancellations and source SHA."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import pytest

from quantlab.intraday_kis_venues import VenueProbeError
from quantlab.intraday_krx_execution_state import ScenarioOrder
from quantlab.intraday_krx_order_evidence import (
    _advance, ledger_for_stream, analyze,
)


def event(kind, i, day="2026-10-21", **extra):
    return {"event":kind,"session":day,"slot":i,"order_executed":False,**extra}


def receipt(kind, number, at="2026-10-21T10:00:00+09:00", **extra):
    return {"fixture_origin":"SYNTHETIC_TEST","event_id":f"event-{number}",
            "broker_order_id":"SIM-001","type":kind,"at":at,**extra}


def test_hypothetical_closed_pit_trade_does_not_confirm_flat_or_profit():
    signal=event("SIGNAL_AFTER_COMPLETED_BAR",5)
    entry=event("HYPOTHETICAL_NEXT_OPEN_ENTRY",6,shares=10,price=100000)
    ex=event("HYPOTHETICAL_CLOSE_BEFORE_AUCTION",75)
    record=ledger_for_stream([signal,entry,ex],expected_sessions={"2026-10-21"})
    assert record["requires_external_broker_reconciliation"] is True
    assert record["status"]=="EXPOSURE_UNCONFIRMED"
    assert record["unverified_scenario_shares"]==10
    assert record["unconfirmed_exit_observations"]==1
    assert record["broker_confirmed_holding_shares"] is None
    assert record["realized_pnl"] is None
    assert record["order_sent_to_broker"] is False


def test_later_halt_does_not_clear_unknown_order_or_autosell():
    first=[event("SIGNAL_AFTER_COMPLETED_BAR",5),
           event("HYPOTHETICAL_NEXT_OPEN_ENTRY",6,shares=10,price=100000),
           event("POSITION_FROZEN_AT_CB",30)]
    subsequent=[event("SIGNAL_AFTER_COMPLETED_BAR",4,"2026-10-22"),
                event("HYPOTHETICAL_NEXT_OPEN_ENTRY",5,"2026-10-22",
                      shares=20,price=90000)]
    result=ledger_for_stream(first+subsequent,
                             expected_sessions={"2026-10-21","2026-10-22"})
    assert result["status"]=="HALT_RECONCILIATION_REQUIRED"
    assert result["subsequent_signals_blocked"]==1
    assert result["subsequent_hypothetical_entries_quarantined"]==1
    assert result["unverified_scenario_shares"]==10
    assert result["broker_confirmed_holding_shares"] is None


def test_ambiguous_next_bar_without_price_never_assumes_zero_shares():
    record=ledger_for_stream([
        event("SIGNAL_AFTER_COMPLETED_BAR",5),
        event("AMBIGUOUS_ENTRY_MISSING_BAR",6)],
        expected_sessions={"2026-10-21"})
    assert record["status"]=="GAP_RECONCILIATION_REQUIRED"
    assert record["unverified_scenario_shares"] is None
    assert record["broker_confirmed_holding_shares"] is None
    assert record["realized_pnl"] is None


def test_reject_out_of_order_or_spoofed_order_execution():
    seq=[event("SIGNAL_AFTER_COMPLETED_BAR",5),
         event("HYPOTHETICAL_NEXT_OPEN_ENTRY",6,shares=2,price=10000)]
    with pytest.raises(VenueProbeError,match="Out-of-order"):
        ledger_for_stream(seq+[event("HYPOTHETICAL_CLOSE_BEFORE_AUCTION",4)],
                          expected_sessions={"2026-10-21"})
    spoof=deepcopy(seq)
    spoof[1]["order_executed"]=True
    with pytest.raises(VenueProbeError,match="real order execution"):
        ledger_for_stream(spoof,expected_sessions={"2026-10-21"})
    with pytest.raises(VenueProbeError,match="without a preceding signal"):
        ledger_for_stream(seq[1:],expected_sessions={"2026-10-21"})


def test_synthetic_partial_fill_cancel_halt_reopen_requires_receipts():
    state=ScenarioOrder(10,"SIM-001")
    state.apply(receipt("ACK",1))
    state.apply(receipt("FILL_CUMULATIVE",2,cumulative_shares=3))
    state.apply(receipt("CANCEL_REQUEST",3))
    assert state.summary()["cancel_requested_but_not_confirmed"] is True
    assert state.filled_shares==3 and state.cancelled is False
    state.apply(receipt("VENUE_HALT",4))
    assert state.summary()["manual_reconciliation_required"] is True
    with pytest.raises(VenueProbeError,match="Cannot confirm new fill"):
        state.apply(receipt("FILL_CUMULATIVE",5,cumulative_shares=4))
    state.apply(receipt("VENUE_REOPEN",6))
    assert state.summary()["manual_reconciliation_required"] is True
    assert state.filled_shares==3
    with pytest.raises(VenueProbeError,match="Cannot confirm new fill"):
        state.apply(receipt("FILL_CUMULATIVE",7,cumulative_shares=4))
    state.apply(receipt("ORDER_STATUS_SNAPSHOT",8,
                        cumulative_shares=4,confirmed_status="OPEN"))
    assert state.summary()["manual_reconciliation_required"] is False
    state.apply(receipt("CANCEL_CONFIRM",9,cumulative_shares=5))
    result=state.summary()
    assert result["cancelled_confirmed_in_fixture"] is True
    assert result["synthetically_confirmed_cumulative_fills"]==5
    assert result["unfilled_shares"]==5
    assert result["actual_filled_shares"] is None
    assert result["trading_authorized"] is False
    assert result["realized_pnl"] is None


def test_duplicate_delivery_is_idempotent_but_conflicts_rejected():
    ledger=ScenarioOrder(2,"SIM-001")
    ack=receipt("ACK",1)
    ledger.apply(ack)
    ledger.apply(ack)
    assert len(ledger.transitions)==1
    changed={**ack,"type":"VENUE_HALT"}
    with pytest.raises(VenueProbeError,match="conflicting"):
        ledger.apply(changed)
    with pytest.raises(VenueProbeError,match="External broker"):
        ledger.apply({**receipt("VENUE_HALT",2),"fixture_origin":"BROKER_UNVERIFIED"})
    with pytest.raises(VenueProbeError,match="Out-of-order"):
        ledger.apply(receipt("VENUE_HALT",3,at="2026-10-20T09:00:00+09:00"))


def test_cumulative_quantity_and_cancel_confirmation_never_rewind_fill():
    ledger=ScenarioOrder(10,"SIM-001")
    ledger.apply(receipt("ACK",1))
    ledger.apply(receipt("FILL_CUMULATIVE",2,cumulative_shares=6))
    with pytest.raises(VenueProbeError,match="must increase"):
        ledger.apply(receipt("FILL_CUMULATIVE",3,cumulative_shares=5))
    ledger.apply(receipt("CANCEL_REQUEST",4))
    with pytest.raises(VenueProbeError,match="Final cumulative"):
        ledger.apply(receipt("CANCEL_CONFIRM",5,cumulative_shares=4))
    ledger.apply(receipt("CANCEL_CONFIRM",6,cumulative_shares=6))
    with pytest.raises(VenueProbeError,match="Cannot confirm"):
        ledger.apply(receipt("FILL_CUMULATIVE",7,cumulative_shares=7))


def test_sha_pinned_end_to_end_fails_on_tampered_pit_source(tmp_path):
    folder=tmp_path/"input"
    folder.mkdir()
    sessions=[f"2026-10-{i:02d}" for i in range(1,31)]
    sessions+= [f"2026-09-{i:02d}" for i in range(1,31)]
    sessions.sort()
    # Tampered or malformed sessions should never produce a ledger.
    payload={"status":"PIT_RESEARCH_PROVISIONAL_NO_CERTIFIED_FILLS_OR_PNL",
             "schema":"KRX_PIT_ORDERED_5M_HALT_STATE_REPLAY_V1",
             "all_session_dates_preserved":sessions,
             "population_stock_days":60,
             "real_orders_executed":0,
             "paper_or_live_enabled":False,"usable_for_backtesting":False,
             "broker_fills_verified":False,
             "certified_pnl_or_sharpe_calculated":False,
             "day_level_halt_blacklist_used_in_decision_loop":False,
             "by_symbol_strategy":{}}
    raw=(json.dumps(payload,sort_keys=True)+"\n").encode()
    (folder/"pit_replay.json").write_bytes(raw)
    (folder/"pit_replay.sha256").write_text(sha256(raw).hexdigest()+"\n")
    with pytest.raises(VenueProbeError,match="population"):
        analyze(pit_dir=folder,output_dir=tmp_path/"output")
    assert not (tmp_path/"output").exists()
    (folder/"pit_replay.json").write_bytes(raw+b"CORRUPTED")
    with pytest.raises(VenueProbeError,match="checksum"):
        analyze(pit_dir=folder,output_dir=tmp_path/"output")


def test_fully_filled_fixture_cannot_request_cancellation_or_reopen_status():
    order=ScenarioOrder(3,"SIM-001")
    order.apply(receipt("ACK",1))
    order.apply(receipt("FILL_CUMULATIVE",2,cumulative_shares=3))
    with pytest.raises(VenueProbeError,match="terminal order"):
        order.apply(receipt("CANCEL_REQUEST",3))
    assert order.filled_shares==3
    closed=ScenarioOrder(3,"SIM-001")
    closed.apply(receipt("ACK",11))
    closed.apply(receipt("CANCEL_REQUEST",12))
    closed.apply(receipt("CANCEL_CONFIRM",13,cumulative_shares=1))
    with pytest.raises(VenueProbeError,match="halted/unknown"):
        closed.apply(receipt("ORDER_STATUS_SNAPSHOT",14,
                             confirmed_status="OPEN",cumulative_shares=1))
    assert closed.cancelled is True

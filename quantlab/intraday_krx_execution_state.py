"""Deterministic KRX broker-receipt *fixture* state machine, never broker I/O.

Supports acknowledgements, cumulative partial fills, cancel requests and
confirmations, market halts and reopening reconciliation. Every event is a
synthetic test fixture; production signals MUST NOT call this to assert fills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .intraday_kis_venues import VenueProbeError

TYPES={
    "ACK", "FILL_CUMULATIVE", "CANCEL_REQUEST",
    "CANCEL_CONFIRM", "VENUE_HALT", "VENUE_REOPEN", "ORDER_STATUS_SNAPSHOT",
}

@dataclass
class ScenarioOrder:
    requested_shares:int
    broker_order_id:str
    acknowledged:bool=False
    filled_shares:int=0
    cancelled:bool=False
    cancel_requested:bool=False
    halted:bool=False
    reconciliation_required:bool=False
    last_at:str|None=None
    seen:dict[str,str]=field(default_factory=dict)
    transitions:list[dict]=field(default_factory=list)

    def __post_init__(self)->None:
        if (isinstance(self.requested_shares,bool) or
            not isinstance(self.requested_shares,int) or
            self.requested_shares<=0 or self.requested_shares>1_000_000 or
            not isinstance(self.broker_order_id,str) or
            not 1<=len(self.broker_order_id)<=128):
            raise VenueProbeError("Invalid fixture order identity or quantity")

    def apply(self, event:dict[str,Any]) -> None:
        """Apply only synthetic test receipts; never verify broker authenticity."""
        if not isinstance(event,dict) or event.get("fixture_origin")!="SYNTHETIC_TEST":
            raise VenueProbeError("External broker evidence not authorized")
        event_id=event.get("event_id")
        event_type=event.get("type")
        stamp=event.get("at")
        if (not isinstance(event_id,str) or not 1<=len(event_id)<=128 or
            event_type not in TYPES or not isinstance(stamp,str) or
            not isinstance(event.get("broker_order_id"),str) or
            event["broker_order_id"]!=self.broker_order_id):
            raise VenueProbeError("Malformed or wrong-order receipt")
        try:
            current=datetime.fromisoformat(stamp)
            if current.tzinfo is None or current.utcoffset() is None:
                raise ValueError("naive")
        except ValueError as exc:
            raise VenueProbeError("Receipt time must be aware ISO") from exc
        canonical=repr(sorted(event.items()))
        if event_id in self.seen:
            if self.seen[event_id]!=canonical:
                raise VenueProbeError("Receipt identity reused with conflicting data")
            return  # duplicate delivery is exactly once
        if self.last_at and current < datetime.fromisoformat(self.last_at):
            raise VenueProbeError("Out-of-order synthetic broker receipts")
        quantity=event.get("cumulative_shares")
        if event_type=="ACK":
            if self.acknowledged or self.cancelled:
                raise VenueProbeError("Duplicate ACK transition")
            self.acknowledged=True
        elif event_type=="FILL_CUMULATIVE":
            if self.halted or self.cancelled or self.reconciliation_required or not self.acknowledged:
                raise VenueProbeError("Cannot confirm new fill while venue/order unverified")
            if (isinstance(quantity,bool) or not isinstance(quantity,int) or
                not self.filled_shares<quantity<=self.requested_shares):
                raise VenueProbeError("Cumulative fill must increase within limit")
            self.filled_shares=quantity
        elif event_type=="CANCEL_REQUEST":
            if (not self.acknowledged or self.cancelled or
                self.filled_shares==self.requested_shares):
                raise VenueProbeError("Cannot request cancellation of unknown/terminal order")
            self.cancel_requested=True  # request != broker confirmation
        elif event_type=="CANCEL_CONFIRM":
            if not self.cancel_requested or self.cancelled or self.halted:
                raise VenueProbeError("Cannot assert cancelled without acknowledged request")
            if (isinstance(quantity,bool) or not isinstance(quantity,int) or
                quantity<self.filled_shares or quantity>self.requested_shares):
                raise VenueProbeError("Final cumulative fill missing or below prior")
            self.filled_shares=quantity
            self.cancelled=True
            self.cancel_requested=False
            self.reconciliation_required=False
        elif event_type=="VENUE_HALT":
            if self.halted:
                raise VenueProbeError("Duplicate market-halt transition")
            self.halted=True
            self.reconciliation_required=True
        elif event_type=="VENUE_REOPEN":
            if not self.halted:
                raise VenueProbeError("Unexpected venue reopening")
            self.halted=False
            # Reopening alone neither fills nor cancels any quantity.
            self.reconciliation_required=True
        elif event_type=="ORDER_STATUS_SNAPSHOT":
            if self.halted or not self.acknowledged or self.cancelled:
                raise VenueProbeError("Order status cannot reconcile halted/unknown order")
            if event.get("confirmed_status")!="OPEN":
                raise VenueProbeError("Only still-open status may be reconciled here")
            if (isinstance(quantity,bool) or not isinstance(quantity,int) or
                not self.filled_shares<=quantity<=self.requested_shares):
                raise VenueProbeError("Cumulative status below acknowledged history")
            self.filled_shares=quantity
            self.reconciliation_required=False
        else:
            raise VenueProbeError("Unsupported test receipt")

        self.last_at=stamp
        self.seen[event_id]=canonical
        self.transitions.append({
            "at":stamp, "type":event_type,
            "filled_shares":self.filled_shares,
            "remaining_shares":self.requested_shares-self.filled_shares,
            "halted":self.halted,
            "reconciliation_required":self.reconciliation_required,
        })

    def summary(self) -> dict:
        return {
            "scenario_only_not_actual_broker_order":True,
            "synthetic_receipt_count":len(self.seen),
            "requested_shares":self.requested_shares,
            "synthetically_confirmed_cumulative_fills":self.filled_shares,
            "unfilled_shares":self.requested_shares-self.filled_shares,
            "cancel_requested_but_not_confirmed":self.cancel_requested,
            "cancelled_confirmed_in_fixture":self.cancelled,
            "venue_halted":self.halted,
            "manual_reconciliation_required":self.reconciliation_required,
            "auto_requote_or_market_exit":False,
            "actual_filled_shares":None,
            "realized_pnl":None,
            "trading_authorized":False,
        }

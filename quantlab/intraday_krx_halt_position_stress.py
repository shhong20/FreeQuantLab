"""Read-only KRX circuit-breaker position/exit stress replay.

This module reports observational before/after market-halt discontinuities
and the inability to transact while the venue is halted. It DOES NOT assume
fills, close existing shares, calculate PnL, or feed unverified KIS minute
timestamps to a backtest. Holding size is a hypothetical stress fixture.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile

from .intraday_krx_halt_5m import EXPECTED_BUCKETS, _event
from .intraday_kis_venues import VenueProbeError


def _read_verified(root: Path, name: str) -> object:
    file=root/name
    digest=root/(name.removesuffix(".json")+".sha256")
    try:
        body=file.read_bytes()
        checksum=digest.read_text(encoding="utf-8").strip()
        if sha256(body).hexdigest()!=checksum:
            raise VenueProbeError("Halt research source checksum mismatch")
        return json.loads(body)
    except (OSError,UnicodeError,ValueError,TypeError) as exc:
        if isinstance(exc,VenueProbeError):
            raise
        raise VenueProbeError("Invalid immutable halt research input") from exc


def _validated(root: Path, symbol: str, expected_sessions: list[str]) -> list[dict]:
    content=_read_verified(root,f"{symbol}_halt_5m_UNVERIFIED.json")
    if not isinstance(content,list) or len(content)!=len(expected_sessions):
        raise VenueProbeError("Halt source session count mismatch")
    for expected,record in zip(expected_sessions,content):
        if (not isinstance(record,dict) or record.get("symbol")!=symbol
            or record.get("session")!=expected or record.get("source_market")!="KRX_J"
            or record.get("reported_cb")!=_event(expected)):
            raise VenueProbeError("Halt source identity or external event mismatch")
        if not isinstance(record.get("source_minute_sha256"),str) or not re.fullmatch(
                r"[0-9a-f]{64}",record["source_minute_sha256"]):
            raise VenueProbeError("Halt source original SHA not pinned")
        slots=record.get("slots")
        if not isinstance(slots,list) or len(slots)!=EXPECTED_BUCKETS:
            raise VenueProbeError("Halt source must preserve all 76 day slots")
        if any(not isinstance(s,dict) or s.get("timestamp")!=
                f"{expected}T{(9*60+5*i)//60:02d}:{(9*60+5*i)%60:02d}:00+09:00"
                for i,s in enumerate(slots)):
            raise VenueProbeError("Halt source 5m time grid is incoherent")
        for slot in slots:
            if slot.get("continuous_order_execution_allowed") is not False:
                raise VenueProbeError("Upstream proposed live order eligibility")
            candle=slot.get("ohlcv")
            if candle is not None:
                if (slot.get("source_observed_count")!=5
                    or not isinstance(candle,dict) or
                    any(not isinstance(candle.get(key),int) or
                            isinstance(candle.get(key),bool) or candle[key]<=0
                            for key in ("open","high","low","close")) or
                    not isinstance(candle.get("volume"),int) or
                    candle["volume"]<0 or
                    candle["high"]<max(candle["open"],candle["low"],candle["close"]) or
                    candle["low"]>min(candle["open"],candle["high"],candle["close"])):
                    raise VenueProbeError("Unusable unverified 5m candidate")
            elif slot.get("source_observed_count")==5:
                raise VenueProbeError("Unexplained missing OHLCV on full source slot")
    return content


def replay(*, input_root: str | Path, out_dir: str | Path,
           assumed_held_shares: int = 10) -> dict:
    if (isinstance(assumed_held_shares,bool) or
        not isinstance(assumed_held_shares,int) or
        not 0<=assumed_held_shares<=100000):
        raise VenueProbeError("Hypothetical shares must be bounded and nonnegative")
    source=Path(input_root).expanduser().resolve()
    target=Path(out_dir).expanduser().resolve()
    if target.exists():
        raise VenueProbeError("Immutable stress report already exists")
    summary=_read_verified(source,"halt_5m_summary.json")
    if (not isinstance(summary,dict) or
        summary.get("status")!="HALT_AWARE_5M_CANDIDATES_RESEARCH_ONLY_NO_PNL"
        or summary.get("source")!="SHA_VERIFIED_KIS_KRX_J_ORIGINAL_MINUTES"
        or summary.get("usable_for_backtesting") is not False
        or summary.get("usable_for_paper_or_live") is not False):
        raise VenueProbeError("Halt research input was not risk-gated")
    symbols=summary.get("symbols")
    sessions=summary.get("sessions")
    if (not isinstance(symbols,list) or len(symbols)!=len(set(symbols))
        or not 1<=len(symbols)<=3 or
        any(not isinstance(s,str) or not re.fullmatch(r"[0-9]{6}",s) for s in symbols)
        or not isinstance(sessions,list) or len(sessions)!=len(set(sessions))
        or not 1<=len(sessions)<=120 or sessions!=sorted(sessions)
        or any(not isinstance(s,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",s)
               for s in sessions)
        or summary.get("total_stock_days")!=len(symbols)*len(sessions)
        or summary.get("total_5m_clock_slots")!=len(symbols)*len(sessions)*EXPECTED_BUCKETS):
        raise VenueProbeError("Incoherent risk-gated source population")
    counts=Counter()
    risk_episodes=[]
    for symbol in symbols:
        days=_validated(source,symbol,sessions)
        for day in days:
            counts[day["day_disposition"]]+=1
            event=day["reported_cb"]
            if not event:
                continue
            if day["day_disposition"]!="ENTIRE_DAY_STRATEGY_PNL_BLOCKED_MARKET_HALT_RISK":
                raise VenueProbeError("Halt day missing mandatory entire-day PnL block")
            activated=datetime.fromisoformat(event["activation_kst"])
            until=datetime.fromisoformat(
                event["conservative_continuous_order_blackout_until_kst"]
            )
            if until-activated != timedelta(minutes=30):
                raise VenueProbeError("Reported market halt interval contract changed")
            slots=day["slots"]
            before=[
                s for s in slots
                if s["ohlcv"] is not None and
                datetime.fromisoformat(s["timestamp"])+timedelta(minutes=5)<=activated
            ]
            after=[
                s for s in slots
                if s["ohlcv"] is not None and
                datetime.fromisoformat(s["timestamp"])>=until
            ]
            earlier=before[-1] if before else None
            later=after[0] if after else None
            pre_close=earlier["ohlcv"]["close"] if earlier else None
            post_open=later["ohlcv"]["open"] if later else None
            indicative_change=(
                post_open/pre_close-1.0
                if isinstance(pre_close,(int,float)) and pre_close>0
                and isinstance(post_open,(int,float)) and post_open>0
                else None
            )
            risk_episodes.append({
                "symbol":symbol,
                "session":day["session"],
                "public_halt_report":event,
                "hypothetical_initial_held_shares":assumed_held_shares,
                "hypothetical_final_held_shares":assumed_held_shares,
                "hypothetical_exit_order_at_activation":{
                    "timestamp":activated.isoformat(),
                    "requested_shares":assumed_held_shares,
                    "decision":"NOT_SUBMITTED_MARKET_HALTED_OR_CALL_AUCTION_UNVERIFIED",
                    "filled_shares":0,
                    "order_left_queued":False,
                    "assumed_position_liquidated":False,
                },
                "post_halt_entry_or_exit":{
                    "decision":"MANUAL_REASSESSMENT_REQUIRED_NO_AUTO_REQUOTE",
                    "filled_shares":0,
                    "order_left_queued":False,
                },
                "last_complete_5m_before_halt_clock":
                    earlier["timestamp"] if earlier else None,
                "last_complete_5m_before_halt_close":pre_close,
                "first_complete_5m_after_reported_blackout_clock":
                    later["timestamp"] if later else None,
                "first_complete_5m_after_reported_blackout_open":post_open,
                "observed_cross_gap_price_change_fraction_not_a_fill_or_pnl":
                    indicative_change,
                "source_5m_time_start_semantics_verified":False,
                "public_event_not_exchange_approved":True,
                "can_estimate_executable_exit_price":False,
                "risk_model_status":"HELD_POSITION_HALT_FILL_NOT_ESTIMATED",
            })
    if len(risk_episodes)!=sum(
            1 for x in summary.get("day_records_without_slots",[])
            if isinstance(x,dict) and x.get("reported_cb")):
        raise VenueProbeError("Halt population count does not match source summary")
    if dict(counts)!=summary.get("day_dispositions"):
        raise VenueProbeError("Halt population status summary mismatch")
    payload={
        "status":"HYPOTHETICAL_HALT_ORDER_POLICY_AND_GAP_OBSERVATION_NO_PNL",
        "source_summary_sha256":sha256(
            (source/"halt_5m_summary.json").read_bytes()
        ).hexdigest(),
        "assumed_held_shares_per_symbol":assumed_held_shares,
        "source_stock_days":len(symbols)*len(sessions),
        "all_source_days_preserved":True,
        "source_day_dispositions":dict(counts),
        "halt_exposure_scenarios":risk_episodes,
        "scenario_count":len(risk_episodes),
        "market_halt_days_not_excluded_from_original_test_population":True,
        "positions_never_automatically_liquidated_at_missing_bar":True,
        "unfilled_orders_never_assumed_executed_on_reopening":True,
        "observational_price_jumps_are_not_realized_returns":True,
        "cash_or_equity_curve_computed":False,
        "sharpe_or_mdd_computed":False,
        "candidate_strategy_selected":False,
        "usable_for_backtesting":False,
        "usable_for_paper_or_live":False,
    }
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".krx-halt-stress-",dir=target.parent))
    try:
        data=(json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (stage/"stress_report.json").write_bytes(data)
        (stage/"stress_report.sha256").write_text(sha256(data).hexdigest()+"\n")
        if target.exists():
            raise VenueProbeError("Concurrent immutable halt stress report")
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(
        description="Offline hypothetical stock-halt stress, zero fills and no PnL"
    )
    parser.add_argument("--input-root",required=True)
    parser.add_argument("--out-dir",required=True)
    parser.add_argument("--assumed-held-shares",type=int,default=10)
    args=parser.parse_args(argv)
    try:
        report=replay(input_root=args.input_root,out_dir=args.out_dir,
                      assumed_held_shares=args.assumed_held_shares)
        print(json.dumps({
            "status":report["status"],
            "source_stock_days":report["source_stock_days"],
            "halt_scenarios":report["scenario_count"],
            "hypothetical_held_shares":report["assumed_held_shares_per_symbol"],
            "observed_price_changes": [
                {
                    "symbol":x["symbol"],"session":x["session"],
                    "cross_gap_fraction":
                    x["observed_cross_gap_price_change_fraction_not_a_fill_or_pnl"],
                } for x in report["halt_exposure_scenarios"]
            ],
            "orders_executed":0,
            "backtesting_allowed":False,
        },ensure_ascii=False,sort_keys=True,indent=2))
        return 0
    except (VenueProbeError,OSError,ValueError,TypeError,KeyError):
        parser.exit(2,"FAIL CLOSED: unverified source or immutable report conflict\n")


if __name__=="__main__":
    raise SystemExit(main())

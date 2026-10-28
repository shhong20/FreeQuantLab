"""KRX displayed-depth sensitivity versus a fixed 5bp price-only hypothesis.

This is a read-only *liquidity stress comparison*, not a trade execution
simulator. KIS market quotations and exchange prints are NOT personal broker
fills. Never update historical backtest results from non-aligned quotes.
All source bytes must be SHA-pinned and KIS response fields independently parsed.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import statistics
import tempfile
from zoneinfo import ZoneInfo

from .intraday_krx_depth_compare import (
    URL, TR_ID, SYMBOLS, parse_quote, sweep_visible, ASSUMED_SLIPPAGE_BPS
)
from .intraday_krx_halt_position_stress import _read_verified
from .intraday_krx_joint_pilot import PILOT_SESSION, WINDOWS
from .intraday_krx_joint_pilot_review import _verify_window
from .intraday_kis_venues import VenueProbeError

KST=ZoneInfo("Asia/Seoul")
SHARE_SIZES=(10,100,500,1000)
NOTIONAL_KRW=(1_000_000,10_000_000,100_000_000)
MAX_STOCK_SNAPSHOTS=100
FIXED_ROUND_TRIP_BPS=2*ASSUMED_SLIPPAGE_BPS


def _load_l2(root:Path)->tuple[list[dict],dict]:
    doc=_read_verified(root,"manifest.json")
    rows=doc.get("quotes")
    if (doc.get("status")!="KIS_KRX_LIVE_L2_READONLY_NO_FILLS" or
        doc.get("symbols")!=list(SYMBOLS) or
        doc.get("real_broker_fills")!=0 or
        doc.get("historical_orderbook_coverage") is not False or
        doc.get("endpoint")!=URL or doc.get("tr_id")!=TR_ID or
        type(doc.get("samples_per_symbol"))!=int or
        not 1<=doc["samples_per_symbol"]<=12 or
        not isinstance(rows,list) or
        len(rows)!=2*doc["samples_per_symbol"]):
        raise VenueProbeError("Untrusted KIS L2 market-data manifest")
    observations=[]
    seen=set()
    for row in rows:
        sy,i=row["symbol"],row["index"]
        if (sy not in SYMBOLS or type(i)!=int or
            not 0<=i<doc["samples_per_symbol"] or (sy,i) in seen or
            row.get("filename")!=f"quote-{i:03d}-{sy}.json" or
            row.get("raw_file")!=f"raw-{i:03d}-{sy}.json"):
            raise VenueProbeError("Invalid quote identity or duplicate")
        seen.add((sy,i))
        normalized=(root/row["filename"]).read_bytes()
        raw=(root/row["raw_file"]).read_bytes()
        if (sha256(normalized).hexdigest()!=row["receipt_sha256"] or
            sha256(raw).hexdigest()!=row["raw_payload_sha256"]):
            raise VenueProbeError("KIS L2 market-data source checksum mismatch")
        record=json.loads(normalized)
        received=datetime.fromisoformat(record["received_utc"])
        independently_parsed=parse_quote(raw,sy,received)
        if (record!=independently_parsed or
            record.get("source_raw_sha256")!=row["raw_payload_sha256"] or
            record.get("KIS_response_broker_fill") is not False):
            raise VenueProbeError("Normalized KIS quotation differs from raw bytes")
        observations.append({"provenance":"SHA_VERIFIED_KIS_L2",
                             "sample_group":"2026-10-28_L2",
                             "sample_id":f"{i:03d}-{sy}",
                             "quote":record})
    return observations,{
        "kind":"standalone_l2",
        "manifest_sha256":sha256((root/"manifest.json").read_bytes()).hexdigest(),
        "verified_market_quotes":len(observations),
    }


def _load_pilot(root:Path)->tuple[list[dict],dict]:
    final=root/f"{PILOT_SESSION}_final_review"
    result=_read_verified(final,"final_review.json")
    if (result.get("session")!=PILOT_SESSION or
        result.get("status") not in (
            "ALL_THREE_WINDOWS_VERIFIED_RESEARCH_ONLY",
            "PARTIAL_OR_FAILED_RESEARCH_PILOT") or
        result.get("paper_or_live_approval") is not False or
        result.get("broker_orders")!=0 or
        result.get("historical_strategy_returns") is not None or
        result.get("expected_windows")!=3):
        raise VenueProbeError("Expected fail-closed KRX one-day pilot closeout")
    indexed=result.get("window_results")
    if (not isinstance(indexed,list) or
        len(indexed)!=3 or
        {x.get("window") for x in indexed}!=set(WINDOWS)):
        raise VenueProbeError("Missing three window outcomes")
    if (result.get("source_verified_windows_not_cross_source_price_certification") is not True or
        result.get("trade_day_independently_certified") is not False or
        result.get("minute_label_start_end_semantics_independently_certified") is not False or
        result.get("safe_to_calibrate_historical_strategy_fills") is not False):
        raise VenueProbeError("KRX source integrity must not certify 3-source fills")
    observations=[]
    for row in indexed:
        window=row["window"]
        if row["status"]!="VERIFIED_RESEARCH_ONLY":
            continue
        checked,verified=_verify_window(root,window,recover_audit=False)
        if (verified is None or checked!=row or
            checked.get("verified_triplets")!=4):
            raise VenueProbeError("Pilot window changed since its independent closeout")
        source=root/f"{PILOT_SESSION}_{window}_raw"
        manifest=_read_verified(source,"manifest.json")
        all_rows=manifest["snapshots"]
        if not isinstance(all_rows,list) or len(all_rows)!=12:
            raise VenueProbeError("Incomplete three-source pilot market data")
        for item in all_rows:
            if item["kind"]!="book":
                continue
            sy=item["symbol"]
            index=item["sample_index"]
            if (sy not in SYMBOLS or type(index)!=int or
                index not in (0,1) or
                item.get("raw_file")!=f"raw-{index:03d}-{sy}-book.json" or
                item.get("normalized_file")!=f"normalized-{index:03d}-{sy}-book.json"):
                raise VenueProbeError("Unexpected KRX pilot book identity")
            raw=(source/item["raw_file"]).read_bytes()
            canonical=(source/item["normalized_file"]).read_bytes()
            if (sha256(raw).hexdigest()!=item["raw_sha256"] or
                sha256(canonical).hexdigest()!=item["normalized_sha256"]):
                raise VenueProbeError("Tampered KRX pilot quote evidence")
            book=json.loads(canonical)
            received=datetime.fromisoformat(item["response_received_utc"])
            assertable=parse_quote(raw,sy,received)
            if (book!=assertable or
                book.get("source_raw_sha256")!=item["raw_sha256"] or
                book.get("KIS_response_broker_fill") is not False or
                book.get("quote_kst","")[:10]!=PILOT_SESSION):
                raise VenueProbeError("KIS book normalized data not independently verified")
            observations.append({
                "provenance":"SHA_VERIFIED_KIS_THREE_SOURCE_PILOT",
                "sample_group":f"2026-10-29_{window}",
                "sample_id":f"{index:03d}-{sy}",
                "quote":book,
            })
    if len(observations)!=4*result["verified_windows"]:
        raise VenueProbeError("Pilot summary and verified book population differ")
    if not observations:
        raise VenueProbeError("No KRX pilot window has source-verified depth")
    return observations,{
        "kind":"joint_pilot",
        "final_report_sha256":sha256((final/"final_review.json").read_bytes()).hexdigest(),
        "verified_market_quotes":len(observations),
        "verified_windows":result["verified_windows"],
        "missing_or_invalid_windows":result["missing_or_invalid_windows"],
        "all_market_sources_verified":result["verified_windows"]==3,
        "tick_minute_observed_price_consistency":
            result["cross_source_price_consistency_observed_only"],
        "trade_day_independently_certified":False,
        "minute_label_start_end_semantics_independently_certified":False,
        "market_observations_are_not_historical_fill_calibration":True,
    }


def _one_sweep(q:dict,side:str,shares:int)->dict:
    data=sweep_visible(q,side,shares)
    result={
        "requested_shares":shares,
        "displayed_depth_sufficient":data["visible_depth_sufficient"],
        "displayed_price_vwap":data["hypothetical_sweep_vwap"],
        "midpoint_adverse_bps":data["visible_adverse_vs_mid_bps"],
        "delta_from_fixed_5bps":(
            data["visible_adverse_vs_mid_bps"]-ASSUMED_SLIPPAGE_BPS
            if data["visible_depth_sufficient"] else None),
        "visible_shares_at_or_better_than_tenth":
            data["visible_shares_at_or_better_than_tenth"],
    }
    return result


def _cost_record(item:dict)->dict:
    q=item["quote"]
    mid=(q["best_ask"]+q["best_bid"])/2
    spread=(q["best_ask"]-q["best_bid"])/mid*1e4
    by_shares={}
    for amount in SHARE_SIZES:
        buy=_one_sweep(q,"BUY",amount)
        sell=_one_sweep(q,"SELL",amount)
        by_shares[str(amount)]={
            "BUY":buy,"SELL":sell,
            "same_snapshot_hypothetical_roundtrip_bps":(
                buy["midpoint_adverse_bps"]+sell["midpoint_adverse_bps"]
                if buy["displayed_depth_sufficient"] and sell["displayed_depth_sufficient"]
                else None),
        }
    by_cash={}
    for cash in NOTIONAL_KRW:
        # Side-specific 1st quote determines a realistic number of whole shares
        buy_shares=cash//q["best_ask"]
        sell_shares=cash//q["best_bid"]
        if buy_shares:
            buy=_one_sweep(q,"BUY",buy_shares)
        else:
            buy={"requested_shares":0,"displayed_depth_sufficient":False,
                 "midpoint_adverse_bps":None,"delta_from_fixed_5bps":None,
                 "displayed_price_vwap":None,
                 "visible_shares_at_or_better_than_tenth":0}
        if sell_shares:
            sell=_one_sweep(q,"SELL",sell_shares)
        else:
            sell={"requested_shares":0,"displayed_depth_sufficient":False,
                  "midpoint_adverse_bps":None,"delta_from_fixed_5bps":None,
                  "displayed_price_vwap":None,
                  "visible_shares_at_or_better_than_tenth":0}
        by_cash[str(cash)]={
            "BUY":buy,"SELL":sell,
            "requested_krw_budget":cash,
            "price-only_not_commissions_or_tax":True,
        }
    return {
        "symbol":q["symbol"],"sample_group":item["sample_group"],
        "sample_id":item["sample_id"],"source_verification":item["provenance"],
        "quote_kst":q["quote_kst"],"received_utc":q["received_utc"],
        "source_raw_sha256":q["source_raw_sha256"],
        "best_bid":q["best_bid"],"best_ask":q["best_ask"],
        "midpoint_krw":mid,"spread_bps":spread,
        "half_spread_bps":spread/2,
        "by_shares":by_shares,"by_budget_krw":by_cash,
        "queue_position_and_cancellations_not_observed":True,
    }


def _distribution(rows:list[dict],metric:str)->dict:
    values=[row[metric] for row in rows]
    return {"n":len(values),
            "median":statistics.median(values) if values else None,
            "minimum":min(values,default=None),
            "maximum":max(values,default=None)}


def analyze(*,output_dir:Path|str, l2_roots:list[Path|str]|None=None,
            pilot_root:Path|str|None=None,
            pit_dir:Path|str|None=None) ->dict:
    target=Path(output_dir).expanduser().resolve()
    if target.exists():
        raise VenueProbeError("Immutable KRX dynamic depth cost comparison exists")
    if not l2_roots and not pilot_root:
        raise VenueProbeError("At least one real KIS raw source required")
    sources=[]
    items=[]
    for root in l2_roots or []:
        path=Path(root).expanduser().resolve()
        observed,src=_load_l2(path)
        sources.append(src)
        items.extend(observed)
    if pilot_root:
        observed,src=_load_pilot(Path(pilot_root).expanduser().resolve())
        sources.append(src)
        items.extend(observed)
    if len(items)>MAX_STOCK_SNAPSHOTS or not items:
        raise VenueProbeError("Unbounded or empty KRX market quote evidence")
    seen=set()
    for item in items:
        key=(item["sample_group"],item["sample_id"])
        if key in seen:
            raise VenueProbeError("Duplicate KRX quote snapshot")
        seen.add(key)
    pit_sha=None
    last_historical=None
    if pit_dir is not None:
        root=Path(pit_dir).expanduser().resolve()
        pit=_read_verified(root,"pit_replay.json")
        historical=pit.get("all_session_dates_preserved")
        if (pit.get("status")!="PIT_RESEARCH_PROVISIONAL_NO_CERTIFIED_FILLS_OR_PNL" or
            not isinstance(historical,list) or
            len(historical)!=100 or historical!=sorted(set(historical)) or
            pit.get("broker_fills_verified") is not False or
            pit.get("certified_pnl_or_sharpe_calculated") is not False):
            raise VenueProbeError("Unverified historical PIT provenance")
        last_historical=historical[-1]
        pit_sha=sha256((root/"pit_replay.json").read_bytes()).hexdigest()
        if any(item["quote"]["quote_kst"][:10]<=last_historical for item in items):
            raise VenueProbeError("No PIT order-by-order quote date alignment")
    records=[_cost_record(item) for item in items]
    groups={}
    for name in sorted({row["sample_group"] for row in records}):
        groups[name]={}
        for sy in SYMBOLS:
            rows=[r for r in records if r["sample_group"]==name and r["symbol"]==sy]
            if not rows:raise VenueProbeError("Unbalanced KRX ticker quote population")
            by_size={}
            for size in SHARE_SIZES:
                key=str(size)
                buy=[r["by_shares"][key]["BUY"] for r in rows]
                sell=[r["by_shares"][key]["SELL"] for r in rows]
                values=[r["by_shares"][key]["same_snapshot_hypothetical_roundtrip_bps"]
                        for r in rows]
                by_size[key]={
                    "BUY_complete_depth_count":sum(r["displayed_depth_sufficient"] for r in buy),
                    "SELL_complete_depth_count":sum(r["displayed_depth_sufficient"] for r in sell),
                    "BUY_cost_bps":_distribution(
                        [{"cost":r["midpoint_adverse_bps"]} for r in buy
                         if r["displayed_depth_sufficient"]],"cost"),
                    "SELL_cost_bps":_distribution(
                        [{"cost":r["midpoint_adverse_bps"]} for r in sell
                         if r["displayed_depth_sufficient"]],"cost"),
                    "roundtrip_same_snapshot_bps":_distribution(
                        [{"cost":v} for v in values if v is not None],"cost"),
                }
            by_budget={}
            for amount in NOTIONAL_KRW:
                k=str(amount)
                by_budget[k]={}
                for side in ("BUY","SELL"):
                    values=[r["by_budget_krw"][k][side] for r in rows]
                    by_budget[k][side]={
                        "complete_depth_count":sum(r["displayed_depth_sufficient"]
                                                   for r in values),
                        "cost_bps":_distribution(
                            [{"cost":r["midpoint_adverse_bps"]} for r in values
                             if r["displayed_depth_sufficient"]],"cost"),
                        "zero_share_budget_count":sum(
                            r["requested_shares"]==0 for r in values),
                    }
            groups[name][sy]={
                "quote_count":len(rows),
                "spread_bps":_distribution(rows,"spread_bps"),
                "halfspread_bps":_distribution(rows,"half_spread_bps"),
                "by_shares":by_size,
                "by_budget_krw":by_budget,
            }
    result={
        "status":"KRX_OBSERVED_L2_PRICE_ONLY_COST_SENSITIVITY_NOT_FILLS",
        "sources":sources,
        "historical_pit_replay_sha256":pit_sha,
        "historical_pit_last_session":last_historical,
        "price_reference":"SAME_SNAPSHOT_BID_ASK_MIDPOINT_NOT_5M_BAR_OPEN",
        "fixed_each_side_slippage_hypothesis_bps":ASSUMED_SLIPPAGE_BPS,
        "fixed_roundtrip_hypothesis_bps":FIXED_ROUND_TRIP_BPS,
        "snapshot_count":len(records),
        "by_sample_group":groups,
        "per_snapshot":records,
        "book_qty_visible_not_guaranteed_fill":True,
        "same_snapshot_roundtrip_not_executable_trade_pair":True,
        "commission_taxes_and_latency_not_included":True,
        "historical_backtest_returns_computed":False,
        "strategy_returns":None,
        "actual_broker_fill_records":0,
        "eligible_for_live_or_paper_trading":False,
    }
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".krx-cost-sensitivity-",dir=target.parent))
    try:
        blob=(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (stage/"cost_sensitivity.json").write_bytes(blob)
        (stage/"cost_sensitivity.sha256").write_text(sha256(blob).hexdigest()+"\n")
        if target.exists():
            raise VenueProbeError("Concurrent immutable KRX cost sensitivity")
        stage.rename(target)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return result


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="Offline KIS L2 vs fixed 5bp sensitivity; no orders")
    p.add_argument("--l2-root",action="append",default=[])
    p.add_argument("--pilot-root")
    p.add_argument("--pit-dir")
    p.add_argument("--output-dir",required=True)
    a=p.parse_args(argv)
    try:
        result=analyze(output_dir=a.output_dir,l2_roots=a.l2_root,
                       pilot_root=a.pilot_root,pit_dir=a.pit_dir)
        print(json.dumps({
            "status":result["status"],
            "snapshot_count":result["snapshot_count"],
            "sample_groups":list(result["by_sample_group"]),
            "by_sample_group":result["by_sample_group"],
            "actual_broker_fill_records":0,
            "strategy_returns":None,
        },ensure_ascii=False,sort_keys=True,indent=2))
        return 0
    except (VenueProbeError,ValueError,KeyError,TypeError,OSError):
        print(json.dumps({"status":"FAIL_CLOSED_NO_VERIFIED_MARKET_COST",
                          "actual_broker_fill_records":0,
                          "strategy_returns":None}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

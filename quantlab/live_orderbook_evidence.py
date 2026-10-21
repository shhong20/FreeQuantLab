"""Read-only Upbit KRW orderbook evidence: fresh snapshots and price-depth sweeps.

No auth, no Exchange API endpoints, no order placement. A displayed L2
snapshot is NOT a guarantee of immediate fill (queue position, latency,
cancellation, market impact and hidden volume are unknown). No historical
orderbook reconstruction from latest orderbook or candle data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
from math import sqrt
from pathlib import Path
import statistics
import time

import requests
import numpy as np
import pandas as pd

from .continuous_portfolio import ASSETS, load_crypto_snapshots
from .daily_receipt import _safe_report

URL="https://api.upbit.com/v1/orderbook"
ORDER_NOTIONALS=(100_000.,1_000_000.,10_000_000.,100_000_000.)
MAX_AGE_SECONDS=12.
MAX_RTT_SECONDS=8.
MIN_CALIBRATION_SAMPLES_PER_ASSET=100
MIN_CALIBRATION_SPAN_HOURS=24.
BASE_SPREAD_BPS=5.
IMPACT_SQRT_BPS=40.


def _utc(s):
    if isinstance(s,datetime):
        return s.astimezone(timezone.utc) if s.tzinfo else s.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(s).replace("Z","+00:00")).astimezone(timezone.utc)


def _check_book(book:dict, received_at:datetime)->dict:
    if not isinstance(book,dict) or book.get("market") not in ASSETS:
        raise ValueError("Missing/unknown book market")
    if book.get("level",0)!=0:
        raise ValueError("Aggregated depth is not valid for ungrouped price sweeps")
    stamp=book.get("timestamp")
    if type(stamp) is not int or stamp<1_500_000_000_000:
        raise ValueError("Missing exchange timestamp")
    age=(received_at-datetime.fromtimestamp(stamp/1000,timezone.utc)).total_seconds()
    if age>MAX_AGE_SECONDS or age < -3:
        raise ValueError(f"Stale/future exchange orderbook: {age:.3f}s")
    units=book.get("orderbook_units")
    if not isinstance(units,list) or not 1<=len(units)<=30:
        raise ValueError("Invalid visible 30-level depth")
    asks=[]; bids=[]
    for u in units:
        if not isinstance(u,dict):
            raise ValueError("Invalid orderbook unit")
        try:
            ap=float(u["ask_price"]); bp=float(u["bid_price"])
            aq=float(u["ask_size"]); bq=float(u["bid_size"])
        except (KeyError,TypeError,ValueError):
            raise ValueError("Missing/invalid depth data")
        if not all(np.isfinite(x) for x in (ap,bp,aq,bq)):
            raise ValueError("Nonfinite orderbook depth")
        if min(ap,bp,aq,bq)<=0:
            raise ValueError("Zero/negative orderbook depth")
        asks.append((ap,aq));bids.append((bp,bq))
    if asks[0][0]<=bids[0][0]:
        raise ValueError("Crossed/locked top-of-book")
    if any(asks[i+1][0]<=asks[i][0] or bids[i+1][0]>=bids[i][0]
           for i in range(len(units)-1)):
        raise ValueError("Unsorted or duplicate book levels")
    return {"market":book["market"],"timestamp_ms":stamp,
            "age_seconds":age,"asks":asks,"bids":bids,
            "best_ask":asks[0][0],"best_bid":bids[0][0],
            "levels":len(units)}


def fetch_public_snapshot(*,session=requests,now=None)->dict:
    # No user secrets or credentials; one public GET for all 4 markets.
    started=_utc(now or datetime.now(timezone.utc))
    response=session.get(URL,params={"markets":",".join(ASSETS),"count":30},
                         headers={"Accept":"application/json",
                                  "User-Agent":"FreeQuantLab-Research/0.2 readonly"},
                         timeout=(3,7))
    received=_utc(now or datetime.now(timezone.utc))
    response.raise_for_status()
    elapsed=(received-started).total_seconds()
    if elapsed<0 or elapsed>MAX_RTT_SECONDS:
        raise ValueError("Request too slow for quote-freshness proof")
    payload=response.content
    if not payload or len(payload)>200_000:
        raise ValueError("Malformed or oversized quote response")
    books=json.loads(payload)
    if not isinstance(books,list) or len(books)!=len(ASSETS):
        raise ValueError("Missing market in batched response")
    validated=[_check_book(item,received) for item in books]
    if set(b["market"] for b in validated)!=set(ASSETS):
        raise ValueError("Duplicated or missing market quote")
    return {"requested_utc":started.isoformat(),
            "received_utc":received.isoformat(),
            "request_rtt_seconds":elapsed,
            "url":URL,"query":{"markets":list(ASSETS),"count":30},
            "raw_payload_sha256":sha256(payload).hexdigest(),
            "raw_payload_utf8":payload.decode("utf-8"),
            "books":books}


def collect_snapshots(*,output_dir:str|Path,count:int=8,spacing_seconds:float=2.,
                      getter=fetch_public_snapshot,sleeper=time.sleep)->dict:
    if type(count)!=int or not 1<=count<=30 or not 1<=spacing_seconds<=120:
        raise ValueError("Bounded snapshot collection parameters only")
    root=Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Quote evidence immutable; refuse overwrite")
    # Write after each complete validated response so partial retrieval can be
    # audited rather than falsely reporting a full successful batch.
    root.mkdir(parents=True,exist_ok=True)
    completed=[]
    for index in range(count):
        if index:
            sleeper(spacing_seconds)
        record=getter()
        recv=_utc(record["received_utc"])
        for item in record["books"]:
            _check_book(item,recv)
        raw=record["raw_payload_utf8"].encode()
        if sha256(raw).hexdigest()!=record["raw_payload_sha256"]:
            raise ValueError("Payload receipt hash inconsistent")
        path=root/f"book-{index:03d}.json"
        _safe_report(path,{k:v for k,v in record.items() if k!="raw_payload_utf8"})
        (root/f"raw-{index:03d}.json").write_bytes(raw)
        completed.append({"index":index,"receipt":path.name,
                          "raw":f"raw-{index:03d}.json",
                          "sha256":record["raw_payload_sha256"],
                          "received_utc":record["received_utc"]})
    result={
        "status":"PUBLIC_READ_ONLY_ORDERBOOK_SNAPSHOTS",
        "endpoint":URL,"expected":count,"captured":len(completed),
        "samples":completed,"real_orders_enabled":False,
        "no_historical_L2_reconstruction":True,
    }
    _safe_report(root/"manifest.json",result)
    return result


def sweep(book:dict,side:str,notional:float)->dict:
    """Immediate displayed-depth quote simulation, no queue or future fills."""
    if side not in ("BUY","SELL") or not np.isfinite(notional) or notional<=0:
        raise ValueError("Invalid sweep intent")
    selected=book["asks"] if side=="BUY" else book["bids"]
    top=selected[0][0]
    # Same base units as KRW notional executed entirely at the top quote.
    requested_units=notional/top
    left=requested_units
    spent=0.
    for price,units in selected:
        qty=min(left,units)
        spent+=qty*price
        left-=qty
        if left<=1e-9*requested_units:
            left=0.
            break
    filled=requested_units-left
    fraction=max(0.,min(1.,filled/requested_units))
    complete=left==0
    midpoint=(book["best_bid"]+book["best_ask"])/2
    best_adverse_bps=(book["best_ask"]/midpoint-1)*1e4 if side=="BUY" else (1-book["best_bid"]/midpoint)*1e4
    if complete:
        vwap=spent/filled
        depth_bps=((vwap/top-1)*1e4 if side=="BUY"
                   else (1-vwap/top)*1e4)
        midpoint_bps=((vwap/midpoint-1)*1e4 if side=="BUY"
                      else (1-vwap/midpoint)*1e4)
    else:
        vwap=depth_bps=midpoint_bps=None
    return {
        "complete_visible_depth":complete,
        "visible_filled_fraction":fraction,
        "requested_at_best_notional_krw":notional,
        "requested_base_units":requested_units,
        "available_visible_base_units":sum(x[1] for x in selected),
        "best_price_krw":top,"best_vs_mid_bps":best_adverse_bps,
        "full_fill_vwap_krw":vwap,"depth_vwap_vs_best_bps":depth_bps,
        "full_fill_vs_mid_bps":midpoint_bps,
    }


def prior_notional_medians(*,cross_snapshot:str|Path,
                           historical_snapshot:str|Path,quote_utc:datetime)->dict:
    frames,_=load_crypto_snapshots(cross_snapshot,historical_snapshot)
    # The historic source freezes through Oct 20: the only fair comparison
    # to this unmodified closed-candle archive is a snapshot on Oct 21 UTC.
    end_day=pd.Timestamp(frames[ASSETS[0]]["date"].iloc[-1]).date()
    if quote_utc.date()!=end_day+timedelta(days=1):
        raise ValueError("Historical candle volume snapshot not current for this quote")
    return {
        s:float(np.median((frames[s]["close"]*frames[s]["volume"]).iloc[-20:]))
        for s in ASSETS
    }


def analyze_snapshots(*,snapshot_dir:str|Path,output_dir:str|Path,
                      volume_medians:dict[str,float]|None=None,
                      sizes:tuple[float,...]=ORDER_NOTIONALS)->dict:
    root=Path(snapshot_dir)
    manifest=json.loads((root/"manifest.json").read_text())
    if manifest.get("status")!="PUBLIC_READ_ONLY_ORDERBOOK_SNAPSHOTS" or manifest["captured"]!=len(manifest["samples"]):
        raise ValueError("Incomplete or invalid quote manifest")
    target=Path(output_dir)
    if target.exists() and any(target.iterdir()):
        raise ValueError("Refuse to overwrite quote analysis")
    if volume_medians is not None and (set(volume_medians)!=set(ASSETS)
        or any(not np.isfinite(v) or v<=0 for v in volume_medians.values())):
        raise ValueError("Invalid prior closed volume evidence")
    values=[]
    seen=set()
    for sample in manifest["samples"]:
        raw=(root/sample["raw"]).read_bytes()
        receipt=json.loads((root/sample["receipt"]).read_text())
        if sha256(raw).hexdigest()!=sample["sha256"] or sample["sha256"]!=receipt["raw_payload_sha256"]:
            raise ValueError("Raw quote evidence SHA mismatch")
        if receipt["received_utc"]!=sample["received_utc"]:
            raise ValueError("Manifest and receipt clock mismatch")
        books=json.loads(raw)
        received=_utc(receipt["received_utc"])
        for b in books:
            clean=_check_book(b,received)
            key=(clean["market"],clean["timestamp_ms"])
            if key in seen:
                raise ValueError("Duplicate exchange timestamp; independent samples not observed")
            seen.add(key)
            for notional in sizes:
                for side in ("BUY","SELL"):
                    measurement=sweep(clean,side,float(notional))
                    proxy=None
                    if volume_medians is not None:
                        median=volume_medians[clean["market"]]
                        proxy=BASE_SPREAD_BPS+IMPACT_SQRT_BPS*sqrt(min(1,notional/median))
                    values.append({
                        "market":clean["market"],"quote_timestamp_ms":clean["timestamp_ms"],
                        "received_utc":received.isoformat(),"age_seconds":clean["age_seconds"],
                        "side":side,"notional_krw":float(notional),
                        "bid_ask_spread_bps":(clean["best_ask"]-clean["best_bid"])/
                                              ((clean["best_ask"]+clean["best_bid"])/2)*1e4,
                        "old_proxy_extra_bps":proxy,
                        **measurement,
                    })
    aggregated=[]
    for market in ASSETS:
        for side in ("BUY","SELL"):
            for size in sizes:
                group=[v for v in values if v["market"]==market and v["side"]==side and
                       v["notional_krw"]==size]
                complete=[v for v in group if v["complete_visible_depth"]]
                arr=[v["full_fill_vs_mid_bps"] for v in complete]
                aggregated.append({
                    "market":market,"side":side,"notional_krw":size,
                    "observations":len(group),"fully_visible_sweeps":len(complete),
                    "incomplete_book_snapshots":len(group)-len(complete),
                    "quote_cost_vs_mid_median_bps":float(np.median(arr)) if arr else None,
                    "quote_cost_vs_mid_p90_bps":float(np.percentile(arr,90)) if arr else None,
                    "min_visible_filled_fraction":min(v["visible_filled_fraction"] for v in group),
                    "old_unfitted_proxy_bps":complete[0]["old_proxy_extra_bps"] if complete else (
                        group[0]["old_proxy_extra_bps"] if group else None),
                })
    timestamps=[_utc(v["received_utc"]) for v in values]
    distinct=len(manifest["samples"])
    hours=(max(timestamps)-min(timestamps)).total_seconds()/3600 if timestamps else 0
    status=("EXPLORATORY_ONLY_NOT_CALIBRATION" if
            distinct<MIN_CALIBRATION_SAMPLES_PER_ASSET or
            hours<MIN_CALIBRATION_SPAN_HOURS else "SNAPSHOT_DEPTH_COVERAGE_GATE_ONLY")
    report={
        "status":status,"raw_capture_count":distinct,"four_market_rows":len(values),
        "span_hours":hours,"required_minimum_samples_per_asset":MIN_CALIBRATION_SAMPLES_PER_ASSET,
        "required_minimum_span_hours":MIN_CALIBRATION_SPAN_HOURS,
        "order_notionals_krw":list(sizes),"raw_source":str(root),
        "quote_sweep_only_not_real_execution":True,
        "does_not_measure_queue_latency_cancellations_hidden_depth":True,
        "not_historical_orderbook_backfill":True,
        "no_model_fitting_or_strategy_PnL_changes":True,
        "book_sweeps":values,"summaries":aggregated,
        "real_orders_enabled":False,"automatic_strategy_promotion":False,
    }
    target.mkdir(parents=True,exist_ok=True)
    _safe_report(target/"results.json",report)
    return report


def main(argv=None)->int:
    p=argparse.ArgumentParser(description="Current public Upbit orderbook quote evidence (never submit orders)")
    p.add_argument("--capture-dir",default="reports/actual-orderbook-20261021")
    p.add_argument("--report-dir",default="reports/actual-orderbook-evaluation-20261021")
    p.add_argument("--samples",type=int,default=8)
    p.add_argument("--interval",type=float,default=2.)
    p.add_argument("--cross-snapshot",default="/home/hong/apps/FreeQuantLab-crossmarket-20261021/reports/cross-market-snapshot-20261021")
    p.add_argument("--historical-snapshot",default="/home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021")
    p.add_argument("--analyze-only",action="store_true")
    args=p.parse_args(argv)
    if not args.analyze_only:
        collect_snapshots(output_dir=args.capture_dir,count=args.samples,
                          spacing_seconds=args.interval)
    manifest=json.loads((Path(args.capture_dir)/"manifest.json").read_text())
    medians=prior_notional_medians(
        cross_snapshot=args.cross_snapshot,historical_snapshot=args.historical_snapshot,
        quote_utc=_utc(manifest["samples"][0]["received_utc"]))
    report=analyze_snapshots(snapshot_dir=args.capture_dir,output_dir=args.report_dir,
                             volume_medians=medians)
    print(json.dumps({
        "status":report["status"],"capture_count":report["raw_capture_count"],
        "span_hours":report["span_hours"],
        "report":str(Path(args.report_dir)/"results.json"),
        "summaries":[x for x in report["summaries"] if x["notional_krw"] in (100_000,10_000_000)],
        "real_orders_enabled":False,
    },ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

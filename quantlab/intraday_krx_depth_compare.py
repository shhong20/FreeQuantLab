"""Bounded, read-only 2026 KRX L2 quote snapshots versus fixed 5bp assumption.

NEVER infer an October 2026 historical fill from an unrelated live quote.
Snapshots are contemporaneous market-data observations, not broker receipts.
KIS production market data GET only: no account/order endpoints, no writes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import tempfile
import time
from zoneinfo import ZoneInfo

import requests

from .data import _kis_token
from .intraday_kis_venues import _get, _decode_response, VenueProbeError
from .intraday_krx_halt_position_stress import _read_verified

URL="https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
TR_ID="FHKST01010200"
SYMBOLS=("005930","000660")
KST=ZoneInfo("Asia/Seoul")
SHARE_SIZES=(10,100,500,1000)
ASSUMED_SLIPPAGE_BPS=5.0
MAX_SNAPSHOTS=12


def _iso(utc: datetime) -> str:
    if not isinstance(utc,datetime) or utc.tzinfo is None or utc.utcoffset() is None:
        raise VenueProbeError("Timezone-aware quote collection required")
    return utc.astimezone(timezone.utc).isoformat()


def parse_quote(raw: bytes, symbol: str, received: datetime) -> dict:
    if symbol not in SYMBOLS:
        raise VenueProbeError("Unsupported KRX comparison symbol")
    data=_decode_response(raw,market="J")
    one,two=data.get("output1"),data.get("output2")
    if not isinstance(one,dict) or not isinstance(two,dict):
        raise VenueProbeError("Missing KIS quote output1/output2")
    if two.get("stck_shrn_iscd")!=symbol:
        raise VenueProbeError("KIS quote ticker identity mismatch")
    label=one.get("aspr_acpt_hour")
    if not isinstance(label,str) or not re.fullmatch(r"[0-2][0-9][0-5][0-9][0-5][0-9]",label):
        raise VenueProbeError("Missing or malformed exchange asking price clock")
    kst=received.astimezone(KST)
    if not (9<=int(label[:2])<=15 and
            (int(label[:2])<15 or int(label[2:4])<20)):
        raise VenueProbeError("Quote outside continuous KRX trading period")
    stamp=kst.replace(hour=int(label[:2]),minute=int(label[2:4]),
                      second=int(label[4:6]),microsecond=0)
    latency=(kst-stamp).total_seconds()
    if not -5<=latency<=20:
        raise VenueProbeError("Stale or future quote not suitable for depth comparison")
    asks=[]
    bids=[]
    for i in range(1,11):
        try:
            ap=int(one[f"askp{i}"]); bp=int(one[f"bidp{i}"])
            aq=int(one[f"askp_rsqn{i}"]); bq=int(one[f"bidp_rsqn{i}"])
        except (KeyError,ValueError,TypeError) as exc:
            raise VenueProbeError("Missing KIS ten-level orderbook fields") from exc
        if min(ap,bp,aq,bq)<=0:
            raise VenueProbeError("Zero or negative displayed depth not usable")
        asks.append([ap,aq]);bids.append([bp,bq])
    if asks[0][0]<=bids[0][0]:
        raise VenueProbeError("Locked or crossed KRX quote")
    if any(asks[i+1][0]<=asks[i][0] or bids[i+1][0]>=bids[i][0]
           for i in range(9)):
        raise VenueProbeError("Unsorted or duplicate KRX quote levels")
    last=two.get("stck_prpr")
    if not isinstance(last,str) or not last.isdigit() or int(last)<=0:
        raise VenueProbeError("KIS last traded price is not numeric")
    return {
        "symbol":symbol,"quote_kst":stamp.isoformat(),
        "received_utc":_iso(received),"quote_age_seconds":latency,
        "source_raw_sha256":sha256(raw).hexdigest(),
        "best_ask":asks[0][0],"best_bid":bids[0][0],
        "last_price":int(last),"asks":asks,"bids":bids,
        "venue":"J","KIS_response_broker_fill":False,
    }


def sweep_visible(quote: dict, side: str, shares: int) -> dict:
    if side not in ("BUY","SELL") or isinstance(shares,bool) or type(shares)!=int or shares<=0:
        raise VenueProbeError("Invalid displayed-depth sweep")
    levels=quote["asks"] if side=="BUY" else quote["bids"]
    left=shares
    cash=0
    for price,amount in levels:
        used=min(left,amount)
        cash+=price*used
        left-=used
        if not left:break
    bid,ask=quote["best_bid"],quote["best_ask"]
    mid=(bid+ask)/2
    best=ask if side=="BUY" else bid
    complete=left==0
    vwap=cash/shares if complete else None
    adverse_mid=(vwap/mid-1)*10000 if complete and side=="BUY" else (
        (1-vwap/mid)*10000 if complete else None)
    adverse_best=(vwap/best-1)*10000 if complete and side=="BUY" else (
        (1-vwap/best)*10000 if complete else None)
    return {
        "side":side,"requested_shares":shares,
        "visible_depth_sufficient":complete,
        "visible_shares_at_or_better_than_tenth":shares-left,
        "hypothetical_sweep_vwap":vwap,
        "visible_adverse_vs_mid_bps":adverse_mid,
        "visible_extra_vs_best_bps":adverse_best,
        "assumed_slippage_bps":ASSUMED_SLIPPAGE_BPS,
        "visible_mid_cost_exceeds_5bps":(
            adverse_mid>ASSUMED_SLIPPAGE_BPS if complete else None),
        "possible_fill_not_certified":True,
    }


def collect(*, output_dir:str|Path, key:str,secret:str,token:str,
            count:int=6,spacing_seconds:float=2.0,
            getter=requests.get,sleeper=time.sleep,
            clock=lambda:datetime.now(timezone.utc)) -> dict:
    dest=Path(output_dir).expanduser().resolve()
    if dest.exists():
        raise VenueProbeError("Immutable KRX L2 snapshot already exists")
    if type(count)!=int or not 1<=count<=MAX_SNAPSHOTS or not 1<=spacing_seconds<=60:
        raise VenueProbeError("Bounded KRX quote sample count/spacing required")
    if not all(isinstance(s,str) and s.strip() for s in (key,secret,token)):
        raise VenueProbeError("KIS read-only market data credentials required")
    dest.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".krx-quote-",dir=dest.parent))
    files=[]
    try:
        for index in range(count):
            if index:sleeper(spacing_seconds)
            for symbol in SYMBOLS:
                now=clock()
                raw=_get(URL,params={"FID_COND_MRKT_DIV_CODE":"J",
                                     "FID_INPUT_ISCD":symbol},
                         key=key,secret=secret,token=token,tr_id=TR_ID,
                         getter=getter)
                recv=clock()
                quote=parse_quote(raw,symbol,recv)
                if (recv-now).total_seconds()>8:
                    raise VenueProbeError("Excessive KRX snapshot roundtrip latency")
                filename=f"quote-{index:03d}-{symbol}.json"
                # Whitelisted market-only receipt, no token or app keys.
                receipt=(json.dumps(quote,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
                (stage/filename).write_bytes(receipt)
                raw_filename=f"raw-{index:03d}-{symbol}.json"
                # KIS market-data RESPONSE ONLY, never HTTP request headers.
                (stage/raw_filename).write_bytes(raw)
                files.append({
                    "filename":filename,"raw_file":raw_filename,
                    "symbol":symbol,"index":index,
                    "receipt_sha256":sha256(receipt).hexdigest(),
                    "raw_payload_sha256":quote["source_raw_sha256"],
                })
        report={
            "status":"KIS_KRX_LIVE_L2_READONLY_NO_FILLS",
            "requested_snapshots_per_symbol":count,
            "samples_per_symbol":count,
            "symbols":list(SYMBOLS),"quotes":files,
            "endpoint":URL,"tr_id":TR_ID,
            "authenticated_KIS_quotation_only":True,
            "historical_orderbook_coverage":False,
            "real_broker_fills":0,
            "usable_for_paper_or_live":False,
        }
        payload=(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (stage/"manifest.json").write_bytes(payload)
        (stage/"manifest.sha256").write_text(sha256(payload).hexdigest()+"\n")
        if dest.exists():
            raise VenueProbeError("Concurrent immutable KRX L2 snapshot")
        stage.rename(dest)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return report


def compare(*, snapshot_dir:str|Path, pit_dir:str|Path,
            output_dir:str|Path) -> dict:
    source=Path(snapshot_dir).expanduser().resolve()
    target=Path(output_dir).expanduser().resolve()
    if target.exists():
        raise VenueProbeError("Immutable KRX execution-depth comparison already exists")
    manifest=_read_verified(source,"manifest.json")
    if (not isinstance(manifest,dict) or
        manifest.get("status")!="KIS_KRX_LIVE_L2_READONLY_NO_FILLS" or
        manifest.get("symbols")!=list(SYMBOLS) or
        manifest.get("endpoint")!=URL or manifest.get("tr_id")!=TR_ID or
        manifest.get("real_broker_fills")!=0):
        raise VenueProbeError("Expected bounded KRX-only L2 quote receipts")
    pit=_read_verified(Path(pit_dir).expanduser().resolve(),"pit_replay.json")
    historical=pit.get("all_session_dates_preserved")
    if (pit.get("status")!="PIT_RESEARCH_PROVISIONAL_NO_CERTIFIED_FILLS_OR_PNL" or
        not isinstance(historical,list) or
        not 60<=len(historical)<=120 or
        pit.get("population_stock_days")!=len(historical)*len(SYMBOLS) or
        pit.get("broker_fills_verified") is not False or
        pit.get("certified_pnl_or_sharpe_calculated") is not False):
        raise VenueProbeError("PIT must remain unverified")
    count=manifest.get("samples_per_symbol")
    receipts=manifest.get("quotes")
    if (type(count)!=int or not 1<=count<=MAX_SNAPSHOTS or
        not isinstance(receipts,list) or len(receipts)!=2*count):
        raise VenueProbeError("Incomplete quote population")
    seen=set()
    groups={sy:[] for sy in SYMBOLS}
    for row in receipts:
        sy,i,filename=row["symbol"],row["index"],row["filename"]
        if (sy not in SYMBOLS or type(i)!=int or not 0<=i<count or
            filename!=f"quote-{i:03d}-{sy}.json" or (i,sy) in seen):
            raise VenueProbeError("Duplicate/incorrect KRX quote identity")
        seen.add((i,sy))
        raw=(source/filename).read_bytes()
        if sha256(raw).hexdigest()!=row["receipt_sha256"]:
            raise VenueProbeError("Tampered KRX quote receipt")
        q=json.loads(raw)
        if (q.get("symbol")!=sy or q.get("venue")!="J" or
            q.get("source_raw_sha256")!=row["raw_payload_sha256"] or
            q.get("KIS_response_broker_fill") is not False):
            raise VenueProbeError("Quoted symbol/source mismatch")
        raw_filename=row.get("raw_file")
        if raw_filename!=f"raw-{i:03d}-{sy}.json":
            raise VenueProbeError("Missing market-only raw quote evidence")
        market_bytes=(source/raw_filename).read_bytes()
        if sha256(market_bytes).hexdigest()!=row["raw_payload_sha256"]:
            raise VenueProbeError("KIS market-data response checksum mismatch")
        q_time=datetime.fromisoformat(q["quote_kst"])
        received=datetime.fromisoformat(q["received_utc"])
        if parse_quote(market_bytes,sy,received)!=q:
            raise VenueProbeError("Normalized depth did not match raw KIS response")
        if (q_time.tzinfo is None or received.tzinfo is None or
            not -5<=(received-q_time).total_seconds()<=20):
            raise VenueProbeError("Untrustworthy quote clock")
        if q_time.date().isoformat() in historical:
            # Historical end is Oct 23. Avoid falsely claiming a matched
            # backtest quote when snapshots overlap by date.
            raise VenueProbeError("Same-date history requires exact-time PIT alignment")
        prices=q["asks"],q["bids"]
        if (len(prices[0])!=10 or len(prices[1])!=10 or
            any(type(p)!=int or type(vol)!=int or p<=0 or vol<=0
                for side in prices for p,vol in side) or
            prices[0][0][0]!=q["best_ask"] or
            prices[1][0][0]!=q["best_bid"] or
            q["best_ask"]<=q["best_bid"]):
            raise VenueProbeError("Unsafe L2 snapshot price-depth structure")
        spread=(q["best_ask"]-q["best_bid"])/(
            (q["best_ask"]+q["best_bid"])/2)*10000
        sweeps={str(n):{side:sweep_visible(q,side,n) for side in ("BUY","SELL")}
                for n in SHARE_SIZES}
        groups[sy].append({
            "quote_kst":q["quote_kst"],
            "spread_bps":spread,
            "mid_price_krw":(q["best_ask"]+q["best_bid"])/2,
            "best_ask":q["best_ask"],"best_bid":q["best_bid"],
            "last_price":q["last_price"],
            "quote_age_seconds":q["quote_age_seconds"],
            "sweeps":sweeps,
        })
    if any(len(groups[sy])!=count for sy in SYMBOLS):
        raise VenueProbeError("Missing stock comparison quotes")
    summary={}
    for sy,rows in groups.items():
        summary[sy]={
            "snapshot_count":len(rows),
            "first_quote_kst":min(x["quote_kst"] for x in rows),
            "last_quote_kst":max(x["quote_kst"] for x in rows),
            "median_spread_bps":statistics.median(x["spread_bps"] for x in rows),
            "spread_bps_min":min(x["spread_bps"] for x in rows),
            "spread_bps_max":max(x["spread_bps"] for x in rows),
            "by_share_size":{
                str(size):{
                    side:{
                        "complete_visible_depth_snapshots":sum(
                            x["sweeps"][str(size)][side]["visible_depth_sufficient"] for x in rows),
                        "median_mid_adverse_bps":(
                            statistics.median(values) if (values:=[
                                x["sweeps"][str(size)][side]["visible_adverse_vs_mid_bps"]
                                for x in rows
                                if x["sweeps"][str(size)][side]["visible_depth_sufficient"]]
                                ) else None),
                        "observed_mid_adverse_above_assumed_5bps_count":sum(
                            x["sweeps"][str(size)][side]["visible_mid_cost_exceeds_5bps"] is True
                            for x in rows),
                    } for side in ("BUY","SELL")
                } for size in SHARE_SIZES},
        }
    result={
        "status":"KRX_LIVE_DEPTH_VS_5BPS_HYPOTHESIS_NOT_HISTORICAL_FILLS",
        "quote_manifest_sha256":sha256((source/"manifest.json").read_bytes()).hexdigest(),
        "pit_replay_sha256":sha256((Path(pit_dir)/"pit_replay.json").read_bytes()).hexdigest(),
        "historical_session_first":historical[0],
        "historical_session_last":historical[-1],
        "historical_quote_date_alignment":False,
        "measured_live_ask_bid_depth_not_historical_reconstruction":True,
        "fixed_backtest_slippage_bps_each_side":ASSUMED_SLIPPAGE_BPS,
        "sampled_market":"KRX_J",
        "by_symbol":summary,
        "per_snapshot":groups,
        "displayed_depth_not_actual_fill":True,
        "latency_queue_cancellation_and_market_impact_not_measured":True,
        "retrospective_strategy_returns_from_this_depth":None,
        "realized_strategy_returns":None,
        "order_submissions":0,
        "eligible_for_strategy_promotion":False,
    }
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".krx-depth-compare-",dir=target.parent))
    try:
        data=(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (stage/"depth_comparison.json").write_bytes(data)
        (stage/"depth_comparison.sha256").write_text(sha256(data).hexdigest()+"\n")
        if target.exists():
            raise VenueProbeError("Concurrent depth comparison")
        stage.rename(target)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return result


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="KRX live orderbook depth vs assumed 5bp; no fills")
    p.add_argument("--capture-dir")
    p.add_argument("--quote-dir")
    p.add_argument("--pit-dir")
    p.add_argument("--output-dir")
    p.add_argument("--count",type=int,default=6)
    p.add_argument("--execute",action="store_true")
    a=p.parse_args(argv)
    try:
        if a.capture_dir:
            if not a.execute:raise VenueProbeError("Explicit --execute required for KIS read-only quote")
            result=collect(output_dir=a.capture_dir,count=a.count,
                           key=os.getenv("KIS_APP_KEY",""),
                           secret=os.getenv("KIS_APP_SECRET",""),token=_kis_token())
            print(json.dumps({"status":result["status"],"quotes":len(result["quotes"])}))
        elif a.quote_dir and a.pit_dir and a.output_dir:
            result=compare(snapshot_dir=a.quote_dir,pit_dir=a.pit_dir,output_dir=a.output_dir)
            print(json.dumps({"status":result["status"],
                              "by_symbol":result["by_symbol"],
                              "historical_quote_date_alignment":False},
                             ensure_ascii=False,indent=2))
        else:
            raise VenueProbeError("Specify capture or compare mode")
        return 0
    except (VenueProbeError,ValueError,TypeError,KeyError,OSError):
        p.exit(2,"FAIL CLOSED: KRX L2 quotation evidence or chronology invalid\n")

if __name__=="__main__":
    raise SystemExit(main())

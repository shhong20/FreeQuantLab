"""KRX J synchronized-near-time quotation evidence, no order or PnL.

Three distinct KIS market-data endpoints: displayed 10-level book, recent
exchange executions (NOT user fills), today's minute candles (NOT completed
while minute is open). Their sequential GET receipt times are recorded;
no synchronized snapshot or fill is claimed. Outputs are immutable/SHA pinned.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import tempfile
import time
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from .data import _kis_token
from .intraday_kis_venues import _get, _decode_response, VenueProbeError
from .intraday_krx_halt_position_stress import _read_verified
from .intraday_krx_depth_compare import URL as BOOK_URL, TR_ID as BOOK_TR, SYMBOLS, parse_quote

BASE="https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/"
ENDPOINTS={
    "book":(BOOK_URL,BOOK_TR),
    "ticks":(BASE+"inquire-ccnl","FHKST01010300"),
    "minute":(BASE+"inquire-time-itemchartprice","FHKST03010200"),
}
KST=ZoneInfo("Asia/Seoul")


def _now_utc(clock):
    value=clock()
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise VenueProbeError("Clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _trading_time(now:datetime) -> bool:
    local=now.astimezone(KST)
    m=local.hour*60+local.minute
    return local.weekday()<5 and 540<=m<920


def _decode_market(raw:bytes, kind:str, sy:str, received:datetime):
    if kind=="book":
        return parse_quote(raw,sy,received)
    data=_decode_response(raw,market="J")
    rows=data.get("output" if kind=="ticks" else "output2")
    if not isinstance(rows,list) or len(rows)>30 or not rows:
        raise VenueProbeError("Missing/oversized KIS execution or 1m page")
    if kind=="minute":
        head=data.get("output1")
        if not isinstance(head,dict):
            raise VenueProbeError("Missing KIS current minute summary")
    received_kst=received.astimezone(KST)
    parsed=[]
    tick_date_claim_fields_seen=0
    for row in rows:
        if not isinstance(row,dict):
            raise VenueProbeError("Invalid exchange trade row")
        clock=row.get("stck_cntg_hour")
        if not isinstance(clock,str) or not re.fullmatch(r"[0-2][0-9][0-5][0-9][0-5][0-9]",clock):
            raise VenueProbeError("Invalid market time label")
        fields=("stck_prpr","cntg_vol") if kind=="ticks" else (
            "stck_oprc","stck_hgpr","stck_lwpr","stck_prpr","cntg_vol")
        try:
            nums={k:int(row[k]) for k in fields}
        except (TypeError,ValueError,KeyError) as exc:
            raise VenueProbeError("Invalid KIS observed price/volume") from exc
        if any(v<=0 for k,v in nums.items() if k!="cntg_vol") or nums["cntg_vol"]<0:
            raise VenueProbeError("Invalid market price or size")
        # Recent execution API may omit a business date, but whenever
        # supplied it must match the current KRX sampling date.
        if kind=="ticks":
            date_claim=row.get("stck_bsop_date")
            if date_claim not in (None,""):
                tick_date_claim_fields_seen+=1
                if date_claim!=received_kst.strftime("%Y%m%d"):
                    raise VenueProbeError("Other-date exchange trade cannot be compared")
        if kind=="minute":
            if row.get("stck_bsop_date")!=received_kst.strftime("%Y%m%d"):
                raise VenueProbeError("Other-date minute cannot be compared")
            if not (nums["stck_lwpr"]<=min(nums["stck_oprc"],nums["stck_prpr"])
                    <=max(nums["stck_oprc"],nums["stck_prpr"])<=nums["stck_hgpr"]):
                raise VenueProbeError("Invalid 1m OHLC")
        parsed.append({"time_label":clock,**nums})
    # SHA-verified data alone can be a stale quotation. Restrict the most
    # recent ticker/1m label to this collection's market-day clock.
    # The 1-minute label is only a freshness guard; its start/end meaning
    # is still not established, so allow 2 minutes of clock ambiguity.
    labels=[]
    for row in parsed:
        label=row["time_label"]
        try:
            labels.append(received_kst.replace(
                hour=int(label[:2]),minute=int(label[2:4]),
                second=int(label[4:6]),microsecond=0))
        except ValueError as exc:
            raise VenueProbeError("Impossible KRX observed clock") from exc
    latest=max(labels)
    age=(received_kst-latest).total_seconds()
    stale_limit=120 if kind=="ticks" else 300
    future_limit=3 if kind=="ticks" else 120
    if not -future_limit<=age<=stale_limit:
        raise VenueProbeError("Stale or future KRX market evidence not suitable for live comparison")
    # Actual KIS inquire-ccnl may omit stck_bsop_date completely.
    # A near-receipt HHMMSS observation is *not* independently dated evidence.
    # Never synthesize a date from response time and label it provider-proven.
    date_claim_all=(kind=="minute" or tick_date_claim_fields_seen==len(parsed))
    return {
        "kind":kind,"symbol":sy,"rows":parsed,
        "latest_row_time_label":max(row["time_label"] for row in parsed),
        "latest_row_label_age_seconds":age,
        "provider_business_date_field_on_all_rows":date_claim_all,
        "market_date_independently_certified":False,
        "source_is_market_data_not_account_fills":True,
    }


def collect(*,out_dir:str|Path,key:str,secret:str,token:str,
            count:int=2,interval:float=1.,clock=lambda:datetime.now(timezone.utc),
            sleeper=time.sleep,getter=requests.get,
            on_observation:Callable[[str,int,str,str],None]|None=None) -> dict:
    root=Path(out_dir).expanduser().resolve()
    if root.exists():
        raise VenueProbeError("Refuse overwrite immutable KRX joint evidence")
    if (type(count)!=int or not 1<=count<=5 or
        not isinstance(interval,(int,float)) or not 0<=interval<=30 or
        not all(isinstance(v,str) and v.strip() for v in (key,secret,token))):
        raise VenueProbeError("Invalid bounded KRX capture arguments")
    root.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix=".krx-joint-",dir=root.parent))
    manifest=[]
    try:
        for i in range(count):
            if i:sleeper(interval)
            for sy in SYMBOLS:
                for kind,(url,tr_id) in ENDPOINTS.items():
                    start=_now_utc(clock)
                    if not _trading_time(start):
                        raise VenueProbeError("Refuse KRX joint data outside live continuous trading")
                    params={"FID_COND_MRKT_DIV_CODE":"J","FID_INPUT_ISCD":sy}
                    if kind=="minute":
                        params.update({
                            "FID_INPUT_HOUR_1":start.astimezone(KST).strftime("%H%M%S"),
                            "FID_PW_DATA_INCU_YN":"Y",
                            "FID_ETC_CLS_CODE":"",
                        })
                    if on_observation is not None:
                        on_observation("STARTED",i,sy,kind)
                    raw=_get(url,params=params,key=key,secret=secret,
                             token=token,tr_id=tr_id,getter=getter)
                    end=_now_utc(clock)
                    if (end-start).total_seconds()>8:
                        raise VenueProbeError("Slow KIS market-data response")
                    normalized=_decode_market(raw,kind,sy,end)
                    pair=f"{i:03d}-{sy}-{kind}"
                    name=f"raw-{pair}.json"
                    (staging/name).write_bytes(raw)
                    norm=(json.dumps(normalized,sort_keys=True,ensure_ascii=False)+"\n").encode()
                    (staging/f"normalized-{pair}.json").write_bytes(norm)
                    if on_observation is not None:
                        on_observation("VERIFIED",i,sy,kind)
                    manifest.append({
                        "sample_index":i,"symbol":sy,"kind":kind,
                        "raw_file":name,"raw_sha256":sha256(raw).hexdigest(),
                        "normalized_file":f"normalized-{pair}.json",
                        "normalized_sha256":sha256(norm).hexdigest(),
                        "request_start_utc":start.isoformat(),
                        "response_received_utc":end.isoformat(),
                        "tr_id":tr_id,"endpoint":url,
                        "minute_cursor":params.get("FID_INPUT_HOUR_1"),
                    })
        data={
            "status":"READ_ONLY_KRX_J_NEAR_TIME_L2_TICKS_1M_NO_FILLS",
            "symbols":list(SYMBOLS),"rounds":count,
            "snapshots":manifest,"real_orders":0,
            "true_atomic_synchronized_snapshot":False,
            "broker_fills_available":False,
            "minute_label_start_end_verified":False,
            "usable_for_backtesting":False,
        }
        payload=(json.dumps(data,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        (staging/"manifest.json").write_bytes(payload)
        (staging/"manifest.sha256").write_text(sha256(payload).hexdigest()+"\n")
        if root.exists():raise VenueProbeError("Concurrent immutable capture")
        staging.rename(root)
        return data
    finally:
        if staging.exists():shutil.rmtree(staging)


def compare(*,source_dir:str|Path,out_dir:str|Path) -> dict:
    root=Path(source_dir).expanduser().resolve()
    target=Path(out_dir).expanduser().resolve()
    if target.exists():raise VenueProbeError("Immutable joint report already exists")
    doc=_read_verified(root,"manifest.json")
    rows=doc.get("snapshots")
    if (doc.get("status")!="READ_ONLY_KRX_J_NEAR_TIME_L2_TICKS_1M_NO_FILLS" or
        doc.get("symbols")!=list(SYMBOLS) or
        doc.get("real_orders")!=0 or
        doc.get("true_atomic_synchronized_snapshot") is not False or
        doc.get("usable_for_backtesting") is not False or
        type(doc.get("rounds"))!=int or not 1<=doc["rounds"]<=5 or
        not isinstance(rows,list) or len(rows)!=doc["rounds"]*6):
        raise VenueProbeError("Missing bounded joint data")
    groups={}
    times=[]
    for row in rows:
        sy,i,k=row["symbol"],row["sample_index"],row["kind"]
        if sy not in SYMBOLS or k not in ENDPOINTS or type(i)!=int or not 0<=i<doc["rounds"]:
            raise VenueProbeError("Invalid joint response identity")
        key=(i,sy,k)
        if key in groups:raise VenueProbeError("Duplicate KRX joint response")
        pair=f"{i:03d}-{sy}-{k}"
        if (row["raw_file"]!=f"raw-{pair}.json" or
            row["normalized_file"]!=f"normalized-{pair}.json" or
            row["endpoint"]!=ENDPOINTS[k][0] or row["tr_id"]!=ENDPOINTS[k][1]):
            raise VenueProbeError("Non-KRX quotation source")
        start=datetime.fromisoformat(row["request_start_utc"])
        end=datetime.fromisoformat(row["response_received_utc"])
        if (start.tzinfo is None or end.tzinfo is None or
            not _trading_time(start) or not 0<=(end-start).total_seconds()<=8):
            raise VenueProbeError("Untrusted request receipt clock")
        times.append((start,end))
        raw=(root/row["raw_file"]).read_bytes()
        blob=(root/row["normalized_file"]).read_bytes()
        if sha256(raw).hexdigest()!=row["raw_sha256"] or sha256(blob).hexdigest()!=row["normalized_sha256"]:
            raise VenueProbeError("KIS raw/normalized market data checksum mismatch")
        normalized=_decode_market(raw,k,sy,end)
        if json.loads(blob)!=normalized:
            raise VenueProbeError("KIS raw/normalized data contradiction")
        groups[key]=(normalized,start,end)
    comparisons=[]
    for i in range(doc["rounds"]):
        for sy in SYMBOLS:
            book,sb,eb=groups[(i,sy,"book")]
            ticks,st,et=groups[(i,sy,"ticks")]
            minute,sm,em=groups[(i,sy,"minute")]
            if not (sb<=eb<=st<=et<=sm<=em):
                raise VenueProbeError("Market sources are not sequentially sampled")
            if (em-sb).total_seconds()>30:
                raise VenueProbeError("Market sample sources too far apart")
            # Tick is a *recent exchange trade*, never user's order fill.
            spread=(book["best_ask"]-book["best_bid"])/(
                (book["best_ask"]+book["best_bid"])/2)*1e4
            minute_labels={x["time_label"][:4]:x for x in minute["rows"]}
            matched=0
            in_range=0
            tick_at_minute=[]
            for tick in ticks["rows"]:
                minute_key=tick["time_label"][:4]
                candle=minute_labels.get(minute_key)
                if candle is None:continue
                matched+=1
                valid=candle["stck_lwpr"]<=tick["stck_prpr"]<=candle["stck_hgpr"]
                in_range+=int(valid)
                tick_at_minute.append({
                    "tick":tick["time_label"],"minute_label":candle["time_label"],
                    "price_in_observed_candle_range":valid,
                    "minute_bar_may_still_be_open":True,
                })
            comparisons.append({
                "sample_index":i,"symbol":sy,
                "start_kst":sb.astimezone(KST).isoformat(),
                "last_received_kst":em.astimezone(KST).isoformat(),
                "receipt_span_seconds":(em-sb).total_seconds(),
                "book_spread_bps":spread,
                "tick_rows":len(ticks["rows"]),
                "minute_rows":len(minute["rows"]),
                "tick_business_date_field_on_all_rows":ticks[
                    "provider_business_date_field_on_all_rows"],
                "minute_business_date_field_on_all_rows":minute[
                    "provider_business_date_field_on_all_rows"],
                "trade_date_independently_certified":False,
                "tick_latest_label_age_seconds":ticks[
                    "latest_row_label_age_seconds"],
                "minute_latest_label_age_seconds":minute[
                    "latest_row_label_age_seconds"],
                "tick_rows_with_same_HHMM_candle_label":matched,
                "tick_rows_with_price_in_minute_range":in_range,
                "tick_minute_diagnostics":tick_at_minute,
                "start_label_hypothesis_verified":False,
                "end_label_hypothesis_verified":False,
                "volume_total_reconciled":False,
                "same_instant_L2_tick_minute":False,
            })
    result={
        "status":"KRX_NEAR_TIME_THREE_SOURCE_CONSISTENCY_NOT_FILL_VALIDATION",
        "manifest_sha256":sha256((root/"manifest.json").read_bytes()).hexdigest(),
        "sample_triplets":len(comparisons),
        "matched_tick_minute_row_count":sum(x["tick_rows_with_same_HHMM_candle_label"] for x in comparisons),
        "matched_tick_price_within_candle_range_count":sum(x["tick_rows_with_price_in_minute_range"] for x in comparisons),
        "per_sample":comparisons,
        "market_execution_ticks_not_personal_broker_fills":True,
        "sample_triplets_with_all_trade_date_fields":sum(
            int(x["tick_business_date_field_on_all_rows"]) for x in comparisons),
        "sample_triplets_without_trade_date_fields":sum(
            int(not x["tick_business_date_field_on_all_rows"]) for x in comparisons),
        "trade_date_independently_certified":False,
        "current_unfinished_minute_volume_not_verified":True,
        "KIS_minute_start_end_semantics_confirmed":False,
        "order_queue_delay_cancel_and_fill_not_measured":True,
        "historical_100_day_fill_prices_certified":False,
        "strategy_returns":None,
        "broker_orders":0,
        "usable_for_backtesting":False,
        "usable_for_paper_or_live":False,
    }
    target.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix=".krx-joint-audit-",dir=target.parent))
    try:
        raw=(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (staging/"joint_comparison.json").write_bytes(raw)
        (staging/"joint_comparison.sha256").write_text(sha256(raw).hexdigest()+"\n")
        if target.exists():raise VenueProbeError("Concurrent immutable comparison")
        staging.rename(target)
        return result
    finally:
        if staging.exists():shutil.rmtree(staging)


def main(argv:list[str]|None=None) -> int:
    p=argparse.ArgumentParser(description="KRX quotation/tick/minute joint evidence, never orders")
    p.add_argument("--capture-dir")
    p.add_argument("--source-dir")
    p.add_argument("--output-dir")
    p.add_argument("--count",type=int,default=2)
    p.add_argument("--execute",action="store_true")
    a=p.parse_args(argv)
    try:
        if a.capture_dir and a.execute:
            key=os.getenv("KIS_APP_KEY","")
            secret=os.getenv("KIS_APP_SECRET","")
            token=os.getenv("KIS_ACCESS_TOKEN") or _kis_token()
            r=collect(out_dir=a.capture_dir,key=key,
                      secret=secret,token=token,count=a.count)
            print(json.dumps({"status":r["status"],"market_response_count":len(r["snapshots"])}))
        elif a.source_dir and a.output_dir and not a.execute:
            r=compare(source_dir=a.source_dir,out_dir=a.output_dir)
            print(json.dumps({"status":r["status"],"triplets":r["sample_triplets"],
                              "matched":r["matched_tick_minute_row_count"]}))
        else:raise VenueProbeError("Explicit capture --execute or offline compare required")
        return 0
    except (VenueProbeError,ValueError,TypeError,KeyError,OSError):
        p.exit(2,"FAIL CLOSED: KRX joint market evidence unavailable or invalid\n")


if __name__=="__main__":
    raise SystemExit(main())

"""Read-only reconciliation of observed daily stock gaps vs immutable vendor sources.

The two original SQLite databases are exclusively opened with mode=ro.
No historical vendor price mismatch is auto-corrected or used as a trading signal.
"""
from __future__ import annotations

import argparse
import ast
from datetime import date,datetime,timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .stock_daily_observer import UNIVERSE
from .stock_long_history import DEFAULT_HISTORY

DEFAULT_OBSERVATIONS="/home/hong/apps/FreeQuantLab/state/stock_daily/observations.sqlite3"
_FIELDS=("open","high","low","close","volume")
_STATE="VENDOR_HISTORY_GAP_PROVENANCE_UNRESOLVED_NO_TRADING"


def _read_db(p:Path)->sqlite3.Connection:
    if not p.is_file():
        raise ValueError("Stock evidence DB not found")
    con=sqlite3.connect(p.as_uri()+"?mode=ro",uri=True,timeout=20)
    if con.execute("PRAGMA quick_check").fetchone()[0]!="ok":
        con.close()
        raise ValueError("Stock evidence DB integrity failed")
    return con


def _valid_prices(vals:object)->tuple[float,...]:
    if (not isinstance(vals,(tuple,list)) or len(vals)!=5 or
        any(type(v) not in (int,float) or not math.isfinite(v) or v<0
            for v in vals) or any(v<=0 for v in vals[:4])):
        raise ValueError("Invalid source OHLCV")
    return tuple(float(v) for v in vals)


def _extract_source(market:str,ticker:str,day:str,raw:bytes)->tuple[float,...]:
    if market=="KRX":
        obj=ast.literal_eval(raw.decode("utf-8").strip())
        if not isinstance(obj,list) or not obj or tuple(obj[0][:6])!=(
                "날짜","시가","고가","저가","종가","거래량"):
            raise ValueError("Unexpected KRX source header")
        matches=[x for x in obj[1:] if isinstance(x,list) and
                 str(x[0]).replace("-","")==day.replace("-","")]
        if len(matches)!=1:
            raise ValueError("Raw KRX source day not unique")
        return _valid_prices(matches[0][1:6])
    if market=="NASDAQ_NYSE":
        payload=json.loads(raw)
        items=payload["chart"]["result"]
        if len(items)!=1 or items[0]["meta"]["symbol"]!=ticker:
            raise ValueError("Wrong US ticker raw source")
        item=items[0]
        quote=item["indicators"]["quote"][0]
        idx=[i for i,v in enumerate(item["timestamp"])
             if datetime.fromtimestamp(v,ZoneInfo("America/New_York")).date().isoformat()==day]
        if len(idx)!=1:
            raise ValueError("Raw US source day not unique")
        return _valid_prices([quote[field][idx[0]] for field in _FIELDS])
    raise ValueError("Unsupported stock market")


def _kind(original:tuple[float,...],candidate:tuple[float,...])->str:
    close=all(math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-5)
              for a,b in zip(original[:4],candidate[:4]))
    if close:
        return ("VOLUME_ONLY_REVISION" if not math.isclose(
            original[4],candidate[4],rel_tol=1e-9,abs_tol=1e-5)
            else "IDENTICAL_SOURCE")
    ratios=[b/a for a,b in zip(original[:4],candidate[:4])]
    if (all(r>0 for r in ratios) and
        abs(sum(ratios)/4-1)>0.0003 and
        max(ratios)-min(ratios)<0.00005):
        return "COHERENT_OHLC_SCALE_SHIFT_POSSIBLE_ADJUSTMENT_NOT_VERIFIED"
    delta=max(abs(a-b) for a,b in zip(original[:4],candidate[:4]))
    relative=max(abs(a-b)/a for a,b in zip(original[:4],candidate[:4]))
    if delta<=0.011 and relative<=0.00005:
        return "SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED"
    return "MATERIAL_OHLC_PRICE_CONFLICT"


def inspect(*,observations:Path|str=DEFAULT_OBSERVATIONS,
            history:Path|str=DEFAULT_HISTORY,
            since:str="2026-10-20")->dict:
    date.fromisoformat(since)
    obs=Path(observations).expanduser().resolve()
    hist=Path(history).expanduser().resolve()
    if (obs.name!="observations.sqlite3" or
        hist.name!="history.sqlite3" or obs.parent!=hist.parent or
        obs.parent.name!="stock_daily"):
        raise ValueError("Separate stock daily evidence databases required")
    results=[]
    o=_read_db(obs)
    h=_read_db(hist)
    try:
        o.execute("BEGIN")
        h.execute("BEGIN")
        for market,syms in UNIVERSE.items():
            for ticker in syms:
                observed={r[0]:_valid_prices(r[1:]) for r in o.execute(
                    "SELECT session_date,open,high,low,close,volume "
                    "FROM daily_bars WHERE market=? AND ticker=? AND session_date>=?",
                    (market,ticker,since))}
                archived={r[0] for r in h.execute(
                    "SELECT session_date FROM long_bars WHERE market=? AND ticker=? "
                    "AND session_date>=?",(market,ticker,since))}
                for day in sorted(observed.keys()-archived):
                    rows=h.execute(
                        "SELECT c.conflict_type,c.base_sha256,"
                        "c.competing_values_json,r.raw,r.observed_utc "
                        "FROM conflicts c JOIN raw_history r ON "
                        "r.market=c.market AND r.ticker=c.ticker "
                        "AND r.raw_sha256=c.base_sha256 "
                        "WHERE c.market=? AND c.ticker=? AND c.session_date=? "
                        "ORDER BY r.observed_utc DESC,r.id DESC",
                        (market,ticker,day)).fetchall()
                    if len(rows)>128:
                        raise ValueError("Unbounded vendor conflict variants")
                    candidate=[]
                    kinds=set()
                    for conflict_type,digest,raw_json,raw,stamp in rows:
                        if sha256(raw).hexdigest()!=digest:
                            raise ValueError("Invalid immutable stock vendor raw SHA")
                        doc=json.loads(raw_json)
                        source=_valid_prices(doc["source"])
                        baseline=_valid_prices(doc["baseline"])
                        if baseline!=observed[day]:
                            raise ValueError("Conflict baseline differs from immutable observation")
                        parsed=_extract_source(market,ticker,day,raw)
                        if any(not math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-7)
                               for a,b in zip(source,parsed)):
                            raise ValueError("Conflict values disagree with original raw source")
                        kind=_kind(observed[day],source)
                        kinds.add(kind)
                        candidate.append({
                            "recorded_at_utc":stamp,
                            "raw_sha256":digest,
                            "conflict_type":conflict_type,
                            "classification":kind,
                            "observed_ohlc":list(observed[day][:4]),
                            "vendor_ohlc":list(source[:4]),
                            "max_absolute_ohlc_difference":max(abs(a-b) for a,b
                                in zip(observed[day][:4],source[:4])),
                            "max_relative_ohlc_difference_bps":max(abs(a-b)/a*10000
                                for a,b in zip(observed[day][:4],source[:4])),
                            "observed_volume":observed[day][4],
                            "vendor_volume":source[4],
                        })
                    latest=candidate[0] if candidate else None
                    results.append({
                        "market":market,"symbol":ticker,"session":day,
                        "state":"UNRESOLVED_VENDOR_PRICE_GAP_NOT_MODEL_ELIGIBLE",
                        "latest_conflict_classification":(
                            latest["classification"] if latest else "NO_RECORDED_VENDOR_CONFLICT"),
                        "earlier_agreeing_price_snapshot_seen":any(x["classification"]
                            in ("IDENTICAL_SOURCE","VOLUME_ONLY_REVISION") for x in candidate),
                        "vendor_observation_variant_count":len(candidate),
                        "material_price_conflict_unresolved":any(
                            k in kinds for k in (
                                "MATERIAL_OHLC_PRICE_CONFLICT",
                                "COHERENT_OHLC_SCALE_SHIFT_POSSIBLE_ADJUSTMENT_NOT_VERIFIED",
                                "SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED")),
                        "sources":candidate,
                    })
        h.execute("COMMIT");o.execute("COMMIT")
    finally:
        h.close();o.close()
    counts={}
    for v in results:
        key=v["latest_conflict_classification"]
        counts[key]=counts.get(key,0)+1
    return {
        "status":_STATE,
        "since":since,
        "raw_source_sha256_checked_for_every_conflict_variant":True,
        "source_is_independent_unofficial_vendor_not_exchange_certified":True,
        "missing_original_observed_days_in_long_history":len(results),
        "counts_by_latest_classification":dict(sorted(counts.items())),
        "all_missing_days_require_manual_verification":True,
        "missing_days_are_not_automatically_backfilled":True,
        "no_modification_to_immutable_stock_databases":True,
        "confirmed_historical_strategy_returns":None,
        "paper_or_live_trading_eligible":False,
        "broker_orders":0,
        "gaps":results,
    }


def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser(description="Read-only vendor-gap provenance; never approve fills")
    ap.add_argument("--observations",default=DEFAULT_OBSERVATIONS)
    ap.add_argument("--history",default=DEFAULT_HISTORY)
    ap.add_argument("--since",default="2026-10-20")
    ap.add_argument("--report",help="New immutable path; never overwrite")
    args=ap.parse_args(argv)
    result=inspect(observations=args.observations,history=args.history,since=args.since)
    if args.report:
        path=Path(args.report).expanduser().resolve()
        if path.exists():
            raise ValueError("Report already exists (immutable)")
        path.parent.mkdir(parents=True,exist_ok=True)
        raw=(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        # Exclusive creation ensures no report overwrites.
        # Never produce world-readable or silently overwritten audit evidence.
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,"wb") as f:f.write(raw)
        digest=path.with_suffix(path.suffix+".sha256")
        fd=os.open(digest,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,"w") as f:f.write(sha256(raw).hexdigest()+"\n")
    print(json.dumps({k:v for k,v in result.items() if k!="gaps"},
                     ensure_ascii=False,sort_keys=True))
    return 0  # audit succeeded; status explicitly records remaining unverified gaps


if __name__=="__main__":
    raise SystemExit(main())

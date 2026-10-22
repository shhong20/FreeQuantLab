"""Independent read-only Korean/US equities & ETF daily observation research.

PUBLIC third-party endpoints (unofficial Naver / Yahoo). No private API,
broker account, real/paper orders, model promotion, or total-return backtests.
Immutable archived OHLCV rows: historical vendor revisions are flagged rather
than silently rewritten. No fabricated closed-market bars or dividend cash.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime,timedelta,time,timezone
from hashlib import sha256
import fcntl
import json
import math
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

import requests

KST=ZoneInfo("Asia/Seoul")
NY=ZoneInfo("America/New_York")
KR={"005930":"Samsung Electronics","000660":"SK hynix","069500":"KODEX 200 ETF"}
US={"AAPL":"Apple","MSFT":"Microsoft","SPY":"SPDR S&P 500 ETF"}
UNIVERSE={"KRX":KR,"NASDAQ_NYSE":US}
DEFAULT_DB="/home/hong/apps/FreeQuantLab/state/stock_daily/observations.sqlite3"
MAX_BODY=3_000_000
PROVIDER={"KRX":"naver_unofficial_siseJson","NASDAQ_NYSE":"yahoo_unofficial_chart"}
COLUMNS=("source","market","ticker","session_date","open","high","low","close","volume","first_seen_utc","first_payload_sha256")


def utcnow():return datetime.now(timezone.utc)


def iso_utc(now:datetime)->str:
    if now.tzinfo is None:raise ValueError("Timezone-aware clock required")
    return now.astimezone(timezone.utc).isoformat()


def parse_day(s:str)->str:
    if not isinstance(s,str) or not re.fullmatch(r"20\d{6}",s):
        raise ValueError("Invalid market-day label")
    return datetime.strptime(s,"%Y%m%d").date().isoformat()


def validate_bars(rows:list[tuple],*,maximum_day:str)->list[tuple]:
    if not 1<=len(rows)<=250:
        raise ValueError("Empty/oversized public daily source")
    seen=set()
    for row in rows:
        if len(row)!=6:raise ValueError("Invalid OHLCV row shape")
        day,op,hi,lo,cl,vol=row
        if (day in seen or not isinstance(day,str) or day>maximum_day):
            raise ValueError("Duplicate or unclosed daily session")
        seen.add(day)
        if not all(type(x) in (int,float) and math.isfinite(x) for x in (op,hi,lo,cl,vol)):
            raise ValueError("Nonfinite daily bar")
        if (min(op,hi,lo,cl)<=0 or vol<0 or hi<max(op,cl) or lo>min(op,cl) or lo>hi):
            raise ValueError("OHLC range/volume integrity violated")
    dates=[r[0] for r in rows]
    if dates!=sorted(dates):
        raise ValueError("Unsorted source market dates")
    return rows


def provider_cutoff(now:datetime,market:str)->str:
    # KRX can run extended-hours trading. Wait until at least 21:00 KST;
    # Yahoo US quotes are not closed until 18:00 NY time. Conservative.
    if market=="KRX":
        local=now.astimezone(KST)
        d=local.date()
        if local.time()<time(21,0):d-=timedelta(days=1)
    elif market=="NASDAQ_NYSE":
        local=now.astimezone(NY)
        d=local.date()
        if local.time()<time(18,0):d-=timedelta(days=1)
    else:raise ValueError("Unknown exchange")
    return d.isoformat()


def _safe_get(url,*,params,getter):
    if not url.startswith("https://"):
        raise ValueError("HTTPS required")
    response=getter(url,params=params,timeout=15,headers={
        "User-Agent":"Mozilla/5.0 (compatible; FreeQuantLab public read-only research)",
        "Accept":"application/json,text/plain,*/*",
    })
    response.raise_for_status()
    raw=response.content
    if not isinstance(raw,bytes) or not 10<=len(raw)<=MAX_BODY:
        raise ValueError("Invalid public source payload size")
    from urllib.parse import urlparse
    effective=getattr(response,"url",None)
    # Fake tests may not expose URL, but requests response always does.
    target=urlparse(effective or url)
    expected=urlparse(url).hostname
    if target.scheme!="https" or target.hostname!=expected:
        raise ValueError("Untrusted public market source redirect")
    return raw


def fetch_naver(symbol:str,now:datetime,*,getter=requests.get)->tuple[bytes,list[tuple]]:
    if symbol not in KR:raise ValueError("Unsupported Korean market ticker")
    current=now.astimezone(KST).date()
    start=(current-timedelta(days=120)).strftime("%Y%m%d")
    end=current.strftime("%Y%m%d")
    url="https://api.finance.naver.com/siseJson.naver"
    raw=_safe_get(url,params={
        "symbol":symbol,"requestType":"1","startTime":start,
        "endTime":end,"timeframe":"day",
    },getter=getter)
    # Naver returns a JavaScript-like array, not strict JSON. literal_eval
    # is non-executable; payload is additionally bounded by MAX_BODY.
    text=raw.decode("utf-8").strip()
    data=ast.literal_eval(text)
    if not isinstance(data,list) or len(data)<2 or not isinstance(data[0],list):
        raise ValueError("Unexpected Naver chart layout")
    if tuple(data[0][:6])!=("날짜","시가","고가","저가","종가","거래량"):
        raise ValueError("Naver public chart column contract changed")
    cutoff=provider_cutoff(now,"KRX")
    out=[]
    for row in data[1:]:
        if not isinstance(row,list) or len(row)<6:
            raise ValueError("Malformed Naver daily candle")
        day=parse_day(row[0])
        if day>cutoff:continue # still potentially trading
        out.append((day,*[float(v) for v in row[1:6]]))
    return raw,validate_bars(out,maximum_day=cutoff)


def fetch_yahoo(symbol:str,now:datetime,*,getter=requests.get)->tuple[bytes,list[tuple]]:
    if symbol not in US:raise ValueError("Unsupported US market ticker")
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    raw=_safe_get(url,params={"range":"3mo","interval":"1d","events":"div,splits"},
                  getter=getter)
    doc=json.loads(raw)
    outer=doc.get("chart",{})
    series=outer.get("result")
    if outer.get("error") is not None or not isinstance(series,list) or len(series)!=1:
        raise ValueError("Yahoo returned missing/invalid chart")
    result=series[0]
    meta=result.get("meta",{})
    if (meta.get("symbol")!=symbol or meta.get("currency")!="USD" or
        meta.get("exchangeTimezoneName")!="America/New_York"):
        raise ValueError("Yahoo stock metadata mismatch")
    times=result.get("timestamp")
    quotes=result.get("indicators",{}).get("quote")
    if not isinstance(times,list) or not isinstance(quotes,list) or len(quotes)!=1:
        raise ValueError("Incomplete Yahoo timeseries")
    q=quotes[0]
    if any(not isinstance(q.get(k),list) or len(q[k])!=len(times)
           for k in ("open","high","low","close","volume")):
        raise ValueError("Yahoo daily OHLCV column length mismatch")
    cutoff=provider_cutoff(now,"NASDAQ_NYSE")
    out=[]
    for i,stamp in enumerate(times):
        if type(stamp) is not int:
            raise ValueError("Noninteger Yahoo market timestamp")
        day=datetime.fromtimestamp(stamp,NY).date().isoformat()
        if day>cutoff:continue
        fields=[q[k][i] for k in ("open","high","low","close","volume")]
        # Null bars can occur for suspended/no-trading periods. They are
        # never fabricated as zero-volume prior closes or interpolated.
        if any(x is None for x in fields):continue
        out.append((day,*[float(x) for x in fields]))
    return raw,validate_bars(out,maximum_day=cutoff)


def _open(path:Path)->sqlite3.Connection:
    con=sqlite3.connect(path,timeout=20)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=FULL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS source_snapshots(
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       source TEXT NOT NULL,market TEXT NOT NULL,ticker TEXT NOT NULL,
       observed_utc TEXT NOT NULL, payload_sha256 TEXT NOT NULL, raw BLOB NOT NULL,
       parsed_bar_count INTEGER NOT NULL,
       UNIQUE(source,market,ticker,payload_sha256)
    );
    CREATE TABLE IF NOT EXISTS daily_bars(
       source TEXT NOT NULL,market TEXT NOT NULL,ticker TEXT NOT NULL,
       session_date TEXT NOT NULL, open REAL NOT NULL,high REAL NOT NULL,
       low REAL NOT NULL,close REAL NOT NULL,volume REAL NOT NULL,
       first_seen_utc TEXT NOT NULL,first_payload_sha256 TEXT NOT NULL,
       PRIMARY KEY(source,market,ticker,session_date)
    );
    CREATE TABLE IF NOT EXISTS revision_alerts(
       source TEXT NOT NULL,market TEXT NOT NULL,ticker TEXT NOT NULL,
       session_date TEXT NOT NULL,old_values_json TEXT NOT NULL,
       new_values_json TEXT NOT NULL, detected_utc TEXT NOT NULL,
       new_payload_sha256 TEXT NOT NULL,
       PRIMARY KEY(source,market,ticker,session_date,new_payload_sha256)
    );
    CREATE TABLE IF NOT EXISTS fetch_failures(
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       market TEXT NOT NULL,ticker TEXT NOT NULL,observed_utc TEXT NOT NULL,
       error_class TEXT NOT NULL
    );
    """)
    return con


def _volume_only(a,b)->bool:
    """True only if immutable OHLC matches but finalized vendor volume differs."""
    return (len(a)==len(b)==5 and
            all(math.isclose(float(x),float(y),rel_tol=1e-9,abs_tol=1e-5)
                for x,y in zip(a[:4],b[:4])) and
            not math.isclose(float(a[4]),float(b[4]),rel_tol=1e-9,abs_tol=1e-5))


def capture(db:str|Path=DEFAULT_DB,*,now:datetime|None=None,
            naver_getter=requests.get,yahoo_getter=requests.get)->dict:
    now=now or utcnow()
    observed=iso_utc(now)
    dest=Path(db).resolve()
    if dest.name!="observations.sqlite3" or dest.parent.name!="stock_daily":
        raise ValueError("Stocks must use isolated state/stock_daily/observations.sqlite3")
    dest.parent.mkdir(parents=True,exist_ok=True)
    lock=dest.parent/".stock-capture.lock"
    results=[]
    with lock.open("a+b") as fp:
        fcntl.flock(fp,fcntl.LOCK_EX|fcntl.LOCK_NB)
        con=_open(dest)
        try:
            for market,assets in UNIVERSE.items():
                for ticker in assets:
                    source=PROVIDER[market]
                    try:
                        raw,rows=(fetch_naver(ticker,now,getter=naver_getter) if market=="KRX"
                                  else fetch_yahoo(ticker,now,getter=yahoo_getter))
                        h=sha256(raw).hexdigest()
                        added=conflicts=volume_revisions=0
                        # Atomic per-symbol transaction: no partial rows on failure.
                        with con:
                            con.execute(
                                """INSERT OR IGNORE INTO source_snapshots
                                   (source,market,ticker,observed_utc,payload_sha256,raw,parsed_bar_count)
                                   VALUES(?,?,?,?,?,?,?)""",
                                (source,market,ticker,observed,h,raw,len(rows)))
                            for day,op,hi,lo,cl,vol in rows:
                                old=con.execute(
                                    """SELECT open,high,low,close,volume FROM daily_bars
                                       WHERE source=? AND market=? AND ticker=? AND session_date=?""",
                                    (source,market,ticker,day)).fetchone()
                                values=(op,hi,lo,cl,vol)
                                if old is not None:
                                    if any(not math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-7)
                                           for a,b in zip(old,values)):
                                        con.execute(
                                            """INSERT OR IGNORE INTO revision_alerts
                                               VALUES(?,?,?,?,?,?,?,?)""",
                                            (source,market,ticker,day,json.dumps(old),
                                             json.dumps(values),observed,h))
                                        if _volume_only(old,values):
                                            volume_revisions+=1
                                        else:
                                            conflicts+=1
                                else:
                                    con.execute(
                                        "INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                        (source,market,ticker,day,*values,observed,h))
                                    added+=1
                        age_days=(datetime.fromisoformat(provider_cutoff(now,market)).date()-
                                  datetime.fromisoformat(rows[-1][0]).date()).days
                        results.append({"market":market,"ticker":ticker,"status":(
                            "HISTORICAL_PROVIDER_REVISION_BLOCKED" if conflicts else
                            "STALE_MARKET_VENDOR_NO_NEW_DAILY_DATA" if age_days>10 else
                            "VENDOR_VOLUME_REVISION_RETAINED_NO_TRADING" if volume_revisions
                            else "PASS"),
                            "source":source,"latest_day":rows[-1][0],
                            "new_completed_bars":added,"provider_revisions":conflicts,
                            "volume_revisions_retained":volume_revisions,
                            "latest_bar_calendar_age_days":age_days,
                            "raw_sha256":h})
                    except (ValueError,requests.RequestException,KeyError,TypeError,
                            UnicodeDecodeError,OverflowError,SyntaxError,MemoryError,
                            RecursionError) as exc:
                        with con:
                            con.execute(
                                "INSERT INTO fetch_failures(market,ticker,observed_utc,error_class) VALUES(?,?,?,?)",
                                (market,ticker,observed,type(exc).__name__))
                        results.append({"market":market,"ticker":ticker,
                                        "status":"FETCH_OR_VALIDATION_FAILED",
                                        "error_class":type(exc).__name__})
        finally:
            con.close()
    safe=("PASS","VENDOR_VOLUME_REVISION_RETAINED_NO_TRADING")
    degraded=any(item["status"] not in safe for item in results)
    volume_warning=any(item["status"]=="VENDOR_VOLUME_REVISION_RETAINED_NO_TRADING"
                       for item in results)
    return {"status":"STOCK_DATA_DEGRADED_NO_TRADING" if degraded else
            "STOCK_DAILY_CAPTURED_WITH_VOLUME_REVISIONS_NO_TRADING" if volume_warning else
            "STOCK_PUBLIC_DAILY_DATA_CAPTURED_NO_TRADING",
            "observed_at_utc":observed,
            "results":results,
            "eligible_for_dividend_adjusted_total_return":False,
            "fund_corporate_actions_verified":False,
            "third_party_market_data_may_be_revised_or_delayed":True,
            "actual_broker_orders":0,"paper_orders_generated":0,
            "automatic_model_promotion":False,"real_orders_enabled":False}


def inspect(db:str|Path=DEFAULT_DB)->dict:
    p=Path(db).resolve()
    if not p.is_file():
        return {"status":"NO_STOCK_DATA_ARCHIVE","markets":{},
                "real_orders_enabled":False}
    with sqlite3.connect("file:"+str(p)+"?mode=ro",uri=True) as con:
        if con.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("Stock SQLite integrity failure")
        per={}
        for market,assets in UNIVERSE.items():
            per[market]={}
            for ticker in assets:
                source=PROVIDER[market]
                n,lo,hi=con.execute(
                    """SELECT COUNT(*),MIN(session_date),MAX(session_date)
                       FROM daily_bars WHERE market=? AND ticker=? AND source=?""",
                    (market,ticker,source)).fetchone()
                revisions=con.execute(
                    "SELECT old_values_json,new_values_json FROM revision_alerts WHERE market=? AND ticker=?",
                    (market,ticker)).fetchall()
                volume_count=sum(_volume_only(json.loads(a),json.loads(b))
                                 for a,b in revisions)
                revised=len(revisions)
                snaps=con.execute(
                    "SELECT COUNT(*) FROM source_snapshots WHERE market=? AND ticker=?",
                    (market,ticker)).fetchone()[0]
                per[market][ticker]={"stored_completed_bars":n,"first":lo,"last":hi,
                                     "immutable_snapshots":snaps,
                                     "historical_revision_alerts":revised,
                                     "vendor_volume_only_revision_evidence":volume_count,
                                     "historical_price_revision_alerts":revised-volume_count}
        failure=con.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0]
        snapshots=con.execute(
            "SELECT id,raw,payload_sha256 FROM source_snapshots ORDER BY id DESC LIMIT 12"
        ).fetchall()
        proof=all(sha256(raw).hexdigest()==hashval for _,raw,hashval in snapshots)
    return {"status":"READ_ONLY_STOCK_DAILY_AUDIT_NOT_TRADING",
            "markets":per,"fetch_failures_cumulative":failure,
            "last_12_raw_payload_sha256_verified":proof,
            "full_historical_raw_audit_performed":False,
            "dividends_splits_total_return_not_verified":True,
            "unofficial_sources_not_licensed_official_KRX_NYSE_tape":True,
            "auto_market_model_enabled":False,
            "paper_orders_generated":False,"real_orders_enabled":False}


def main(argv=None)->int:
    p=argparse.ArgumentParser(description="Isolated unofficial public KR/US stock research observations")
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument("--capture",action="store_true")
    group.add_argument("--audit",action="store_true")
    p.add_argument("--db",default=DEFAULT_DB)
    args=p.parse_args(argv)
    result=capture(args.db) if args.capture else inspect(args.db)
    print(json.dumps(result,ensure_ascii=False))
    accepted_capture=(
        "STOCK_PUBLIC_DAILY_DATA_CAPTURED_NO_TRADING",
        "STOCK_DAILY_CAPTURED_WITH_VOLUME_REVISIONS_NO_TRADING",
    )
    return (2 if args.capture and result["status"] not in accepted_capture
            or args.audit and not result.get("last_12_raw_payload_sha256_verified",False)
            else 0)


if __name__=="__main__":
    raise SystemExit(main())

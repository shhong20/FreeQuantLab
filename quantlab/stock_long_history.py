"""Six-asset long-horizon PUBLIC vendor history, isolated from live stock and crypto.

2021+ Naver KRX OHLCV; 5-year Yahoo US OHLCV + vendor dividend/split
events and adjusted close. Each byte source and SHA preserved; current
stock_daily/observations.sqlite3 is opened MODE=RO to check every shared
price date. Revisions and mismatches are evidence, not automatic repairs.

Historical bars never authorize total-return claims, frozen Qlib models,
paper fills, or real broker orders. Missing sessions are never fabricated.
"""
from __future__ import annotations
import argparse
import ast
from datetime import datetime,timezone
from hashlib import sha256
import fcntl
import json
import math
from pathlib import Path
import sqlite3

import requests

from .stock_daily_observer import (
    DEFAULT_DB,UNIVERSE,PROVIDER,KR,US,NY,KST,
    _safe_get,parse_day,provider_cutoff,validate_bars,iso_utc,
)
from .stock_evidence_quality import (
    REQUIRED_QLIB_DAILY_SESSIONS, _reference_sessions,
    KNOWN_2026_KRX_REFERENCE_EXCEPTIONS,
)

DEFAULT_HISTORY="/home/hong/apps/FreeQuantLab/state/stock_daily/history.sqlite3"
START_KR="20210101"
MAX_BARS=2200
# Yahoo adjusted close may jitter at Float32 precision. 1e-5 relative
# means 0.1bp; retain every original raw response in all circumstances.
ADJCLOSE_MATERIAL_RELATIVE=1e-5
COLS=("open","high","low","close","volume")

def _validate_all(rows:list[tuple],cutoff:str)->list[tuple]:
    if not 600<=len(rows)<=MAX_BARS:
        raise ValueError("Long stock history outside 600..2200 real session bound")
    for i in range(0,len(rows),250):
        validate_bars(rows[i:i+250],maximum_day=cutoff)
    dates=[r[0] for r in rows]
    if dates!=sorted(set(dates)):
        raise ValueError("Long stock market sessions duplicate/out of order")
    return rows

def fetch_long(market:str,ticker:str,now:datetime,*,getter=requests.get):
    if ticker not in UNIVERSE.get(market,{}):
        raise ValueError("Unsupported long-history stock")
    cutoff=provider_cutoff(now,market)
    if market=="KRX":
        end=now.astimezone(KST).strftime("%Y%m%d")
        raw=_safe_get("https://api.finance.naver.com/siseJson.naver",
            params={"symbol":ticker,"requestType":"1","startTime":START_KR,
                    "endTime":end,"timeframe":"day"},getter=getter)
        data=ast.literal_eval(raw.decode("utf-8").strip())
        if not isinstance(data,list) or not data or tuple(data[0][:6])!=(
            "날짜","시가","고가","저가","종가","거래량"):
            raise ValueError("Unexpected long Korean vendor schema")
        rows=[]
        for item in data[1:]:
            if not isinstance(item,list) or len(item)<6:
                raise ValueError("Invalid long Korean row")
            day=parse_day(item[0])
            if day<=cutoff:
                rows.append((day,*[float(n) for n in item[1:6]]))
        events=[]
        adjusted=[]
    else:
        url=f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        raw=_safe_get(url,params={"range":"5y","interval":"1d","events":"div,splits"},
                      getter=getter)
        payload=json.loads(raw)
        chart=payload.get("chart",{})
        result=chart.get("result")
        if (chart.get("error") is not None or not isinstance(result,list)
            or len(result)!=1):
            raise ValueError("Invalid Yahoo long history response")
        d=result[0]
        meta=d.get("meta",{})
        if (meta.get("symbol")!=ticker or meta.get("currency")!="USD"
            or meta.get("exchangeTimezoneName")!="America/New_York"):
            raise ValueError("Long US vendor identity changed")
        timestamps=d.get("timestamp")
        quotes=d.get("indicators",{}).get("quote")
        if not isinstance(timestamps,list) or not isinstance(quotes,list) or len(quotes)!=1:
            raise ValueError("Invalid US chart dimensions")
        q=quotes[0]
        if any(not isinstance(q.get(k),list) or len(q[k])!=len(timestamps) for k in COLS):
            raise ValueError("Invalid US price vector dimensions")
        adj_list=d.get("indicators",{}).get("adjclose")
        if not isinstance(adj_list,list) or len(adj_list)!=1 or not isinstance(adj_list[0],dict):
            raise ValueError("Vendor adjusted close evidence missing")
        adj_arr=adj_list[0].get("adjclose")
        if not isinstance(adj_arr,list) or len(adj_arr)!=len(timestamps):
            raise ValueError("Adjusted close vector length mismatch")
        rows=[];adjusted=[]
        for i,stamp in enumerate(timestamps):
            if type(stamp) is not int:
                raise ValueError("Invalid US timestamp")
            day=datetime.fromtimestamp(stamp,NY).date().isoformat()
            if day>cutoff:continue
            values=[q[k][i] for k in COLS]
            if any(v is None for v in values):
                continue  # Never generate market sessions for missing bars
            adj=adj_arr[i]
            if type(adj) not in (float,int) or not math.isfinite(adj) or adj<=0:
                raise ValueError("Invalid vendor adjusted price at market session")
            rows.append((day,*[float(v) for v in values]))
            adjusted.append((day,float(adj)))
        events=[]
        evdict=d.get("events") or {}
        if not isinstance(evdict,dict) or not set(evdict).issubset(
            {"dividends","splits","capitalGains"}):
            raise ValueError("Invalid Yahoo event map")
        for kind in ("dividends","splits"):
            ed=evdict.get(kind,{})
            if not isinstance(ed,dict) or len(ed)>150:
                raise ValueError("Too many invalid vendor action events")
            for event in ed.values():
                if not isinstance(event,dict) or type(event.get("date")) is not int:
                    raise ValueError("Invalid vendor corporate action date")
                if kind=="dividends":
                    value=event.get("amount")
                    if type(value) not in (float,int) or not math.isfinite(value) or value<=0:
                        raise ValueError("Invalid vendor cash distribution")
                else:
                    a=event.get("numerator");b=event.get("denominator")
                    if (type(a) not in (float,int) or type(b) not in (float,int)
                        or a<=0 or b<=0):
                        raise ValueError("Invalid vendor split")
                # Exact vendor JSON is retained, not treated as issuer proof.
                encoded=json.dumps(event,sort_keys=True,separators=(",",":"))
                evday=datetime.fromtimestamp(event["date"],NY).date().isoformat()
                events.append((kind,evday,sha256(encoded.encode()).hexdigest(),encoded))
    return raw,_validate_all(rows,cutoff),events,adjusted


def _db(path:Path)->sqlite3.Connection:
    con=sqlite3.connect(path,timeout=20)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS raw_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market TEXT NOT NULL,ticker TEXT NOT NULL,provider TEXT NOT NULL,
        observed_utc TEXT NOT NULL,raw_sha256 TEXT NOT NULL,raw BLOB NOT NULL,
        parsed_sessions INTEGER NOT NULL,
        UNIQUE(market,ticker,provider,raw_sha256)
    );
    CREATE TABLE IF NOT EXISTS long_bars(
        market TEXT NOT NULL,ticker TEXT NOT NULL,session_date TEXT NOT NULL,
        open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,
        close REAL NOT NULL,volume REAL NOT NULL,
        first_raw_sha256 TEXT NOT NULL,
        PRIMARY KEY(market,ticker,session_date)
    );
    CREATE TABLE IF NOT EXISTS vendor_adjusted(
        ticker TEXT NOT NULL,session_date TEXT NOT NULL,
        adjusted_close REAL NOT NULL,first_raw_sha256 TEXT NOT NULL,
        PRIMARY KEY(ticker,session_date)
    );
    CREATE TABLE IF NOT EXISTS vendor_actions(
        ticker TEXT NOT NULL,kind TEXT NOT NULL,
        event_day TEXT NOT NULL,event_sha256 TEXT NOT NULL,
        raw_event_json TEXT NOT NULL,first_raw_sha256 TEXT NOT NULL,
        PRIMARY KEY(ticker,kind,event_day,event_sha256)
    );
    CREATE TABLE IF NOT EXISTS conflicts(
        market TEXT NOT NULL,ticker TEXT NOT NULL,session_date TEXT NOT NULL,
        conflict_type TEXT NOT NULL,base_sha256 TEXT NOT NULL,
        competing_values_json TEXT NOT NULL,
        PRIMARY KEY(market,ticker,session_date,conflict_type,base_sha256)
    );
    CREATE TABLE IF NOT EXISTS failures(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market TEXT NOT NULL,ticker TEXT NOT NULL,observed_utc TEXT NOT NULL,
        error_class TEXT NOT NULL
    );
    """)
    return con


def _diff(a,b)->bool:
    return any(not math.isclose(float(x),float(y),rel_tol=1e-9,abs_tol=1e-5)
               for x,y in zip(a,b))


def capture_history(*,history_db:str|Path=DEFAULT_HISTORY,
                    source_db:str|Path=DEFAULT_DB,now:datetime|None=None,
                    naver_getter=requests.get,yahoo_getter=requests.get)->dict:
    now=now or datetime.now(timezone.utc)
    stamp=iso_utc(now)
    history=Path(history_db).resolve()
    original=Path(source_db).resolve()
    if history.name!="history.sqlite3" or history.parent.name!="stock_daily":
        raise ValueError("Independent long stock history path is required")
    if original.name!="observations.sqlite3" or original.parent.name!="stock_daily":
        raise ValueError("Reject crypto or non-stock baseline comparison database")
    if not original.is_file() or original.parent!=history.parent:
        raise FileNotFoundError("Read-only original stock observations required in same folder")
    with sqlite3.connect("file:"+str(original)+"?mode=ro",uri=True) as c:
        if c.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("Source daily observations failed integrity check")
    history.parent.mkdir(parents=True,exist_ok=True)
    lock=history.parent/".history.lock"
    result=[]
    with lock.open("a+b") as lockfd:
        fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        con=_db(history)
        base=sqlite3.connect("file:"+str(original)+"?mode=ro",uri=True,timeout=20)
        try:
            for market,tickers in UNIVERSE.items():
                for ticker in tickers:
                    getter=naver_getter if market=="KRX" else yahoo_getter
                    try:
                        raw,rows,events,adjusted=fetch_long(market,ticker,now,getter=getter)
                        digest=sha256(raw).hexdigest()
                        base_rows={d:vals for d,*vals in base.execute(
                            """SELECT session_date,open,high,low,close,volume
                               FROM daily_bars WHERE market=? AND ticker=?""",
                            (market,ticker))}
                        overlap=0;different=0;new=0;local_conflicts=0
                        with con:
                            con.execute("""INSERT OR IGNORE INTO raw_history(
                                    market,ticker,provider,observed_utc,raw_sha256,raw,parsed_sessions)
                                    VALUES(?,?,?,?,?,?,?)""",
                                (market,ticker,PROVIDER[market],stamp,digest,raw,len(rows)))
                            for day,*values in rows:
                                prior=base_rows.get(day)
                                if prior is not None:
                                    overlap+=1
                                    if _diff(prior,values):
                                        different+=1
                                        con.execute("INSERT OR IGNORE INTO conflicts VALUES(?,?,?,?,?,?)",
                                            (market,ticker,day,"LIVE_BASELINE_MISMATCH",digest,
                                             json.dumps({"source":values,"baseline":prior})))
                                        # Refuse storing dubious series silently as accepted
                                        continue
                                existing=con.execute(
                                    "SELECT open,high,low,close,volume FROM long_bars WHERE market=? AND ticker=? AND session_date=?",
                                    (market,ticker,day)).fetchone()
                                if existing is None:
                                    con.execute("INSERT INTO long_bars VALUES(?,?,?,?,?,?,?,?,?)",
                                                (market,ticker,day,*values,digest))
                                    new+=1
                                elif _diff(existing,values):
                                    local_conflicts+=1
                                    con.execute("INSERT OR IGNORE INTO conflicts VALUES(?,?,?,?,?,?)",
                                                (market,ticker,day,"LONG_VENDOR_REVISION",digest,
                                                 json.dumps({"source":values,"previous":existing})))
                            for day,adj in adjusted:
                                old=con.execute(
                                    "SELECT adjusted_close FROM vendor_adjusted WHERE ticker=? AND session_date=?",
                                    (ticker,day)).fetchone()
                                if old is None:
                                    con.execute("INSERT INTO vendor_adjusted VALUES(?,?,?,?)",
                                                (ticker,day,adj,digest))
                                elif not math.isclose(old[0],adj,rel_tol=ADJCLOSE_MATERIAL_RELATIVE,abs_tol=1e-5):
                                    con.execute("INSERT OR IGNORE INTO conflicts VALUES(?,?,?,?,?,?)",
                                                (market,ticker,day,"VENDOR_ADJ_CLOSE_REVISION",digest,
                                                 json.dumps({"previous_adj":old[0],"new_adj":adj})))
                                    local_conflicts+=1
                            for kind,day,eventsha,body in events:
                                con.execute("INSERT OR IGNORE INTO vendor_actions VALUES(?,?,?,?,?,?)",
                                            (ticker,kind,day,eventsha,body,digest))
                        result.append({"market":market,"ticker":ticker,
                            "status":"REVIEW_UNMODIFIED_ORIGINAL_AND_VENDOR_PRICE" if different or local_conflicts else "STORED_UNVERIFIED_VENDOR_HISTORY",
                            "sessions_received":len(rows),"first_day":rows[0][0],
                            "last_day":rows[-1][0],"new_immutable_bars":new,
                            "overlap_compared":overlap,
                            "overlap_mismatches":different,
                            "historical_revisions_detected":local_conflicts,
                            "vendor_actions":len(events),"vendor_adjusted_points":len(adjusted),
                            "raw_sha256":digest})
                    except (ValueError,TypeError,KeyError,UnicodeDecodeError,
                            SyntaxError,OverflowError,requests.RequestException) as exc:
                        with con:
                            con.execute("INSERT INTO failures VALUES(NULL,?,?,?,?)",
                                        (market,ticker,stamp,type(exc).__name__))
                        result.append({"market":market,"ticker":ticker,
                                       "status":"SOURCE_FAILED_NOT_QUALIFIED",
                                       "error_class":type(exc).__name__})
        finally:
            con.close();base.close()
    return {
        "status":"LONG_VENDOR_HISTORY_WITH_ISSUER_AND_TOTAL_RETURN_GATE_BLOCKED"
            if all(x["status"]=="STORED_UNVERIFIED_VENDOR_HISTORY" for x in result)
            else "LONG_VENDOR_HISTORY_REVIEW_REQUIRED",
        "observed_at_utc":stamp,"assets":result,
        "source_observation_database_unchanged":True,
        "official_exchange_data_not_confirmed":True,
        "cash_dividends_and_split_adjustment_not_issuer_verified":True,
        "vendor_adjusted_close_not_total_return_proof":True,
        "qlib_frozen_90day_cohort_registered":False,
        "paper_orders":0,"real_orders_enabled":False
    }


def audit_history(db:str|Path=DEFAULT_HISTORY)->dict:
    path=Path(db).resolve()
    if path.name!="history.sqlite3" or path.parent.name!="stock_daily" or not path.is_file():
        raise ValueError("Stock-only long archive missing")
    with sqlite3.connect("file:"+str(path)+"?mode=ro",uri=True) as c:
        if c.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("History SQLite quick_check failed")
        raw=list(c.execute("SELECT raw,raw_sha256 FROM raw_history"))
        complete_raw=all(sha256(blob).hexdigest()==h for blob,h in raw)
        conflicts=c.execute(
            "SELECT conflict_type,competing_values_json FROM conflicts"
        ).fetchall()
        precision_jitter=0
        material_conflicts=0
        for conflict_type,body in conflicts:
            if conflict_type=="VENDOR_ADJ_CLOSE_REVISION":
                x=json.loads(body)
                if math.isclose(x["previous_adj"],x["new_adj"],
                                rel_tol=ADJCLOSE_MATERIAL_RELATIVE,abs_tol=1e-5):
                    precision_jitter+=1
                    continue
            material_conflicts+=1
        failures=c.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        by={}
        for market,tickers in UNIVERSE.items():
            by[market]={}
            for ticker in tickers:
                dates=[d[0] for d in c.execute(
                    "SELECT session_date FROM long_bars WHERE market=? AND ticker=? ORDER BY session_date",
                    (market,ticker))]
                count=len(dates)
                first,last=(dates[0],dates[-1]) if dates else (None,None)
                reference=_reference_sessions(market,first,last) if dates else {
                    "name":"UNKNOWN","version":"NO_DATA","status":"REFERENCE_CALENDAR_NOT_AVAILABLE","sessions":[]}
                expected=set(reference["sessions"])
                observed=set(dates)
                missing=sorted(expected-observed)
                unexpected=sorted(observed-expected)
                explained=sorted(d for d in missing if market=="KRX" and
                                 d in KNOWN_2026_KRX_REFERENCE_EXCEPTIONS)
                other_missing=sorted(set(missing)-set(explained))
                actions=c.execute("SELECT count(*) FROM vendor_actions WHERE ticker=?",(ticker,)).fetchone()[0]
                adjusted=c.execute("SELECT count(*) FROM vendor_adjusted WHERE ticker=?",(ticker,)).fetchone()[0]
                by[market][ticker]={
                    "completed_vendor_daily_sessions":count,"first":first,"last":last,
                    "reference_calendar":reference["name"],
                    "reference_calendar_version":reference["version"],
                    "known_exchange_closure_missing_in_library":explained,
                    "unknown_reference_calendar_missing_sessions":other_missing,
                    "unexpected_vendor_sessions_not_in_reference":unexpected,
                    "reference_is_not_an_official_exchange_attestation":True,
                    "historical_length_threshold_662_met":count>=REQUIRED_QLIB_DAILY_SESSIONS,
                    "vendor_action_events":actions,
                    "vendor_adjusted_close_points":adjusted,
                    "official_corporate_actions_certified":False,
                    "price_total_return_signals_eligible":False}
        all_lengths=all(v["historical_length_threshold_662_met"]
                        for assets in by.values() for v in assets.values())
    return {"status":"LONG_HISTORY_LENGTH_VERIFIED_NOT_MODEL_ELIGIBILITY"
            if all_lengths and complete_raw and not material_conflicts and not failures else
            "LONG_HISTORY_OR_SOURCE_INTEGRITY_REVIEW_REQUIRED",
            "markets":by,"sha256_all_history_source_payloads_verified":complete_raw,
            "stored_raw_snapshots":len(raw),
            "material_price_or_adjustment_conflicts":material_conflicts,
            "retained_vendor_adjustment_precision_jitter_events":precision_jitter,
            "adjusted_close_material_relative_threshold":ADJCLOSE_MATERIAL_RELATIVE,
            "historical_fetch_failures":failures,
            "minimum_model_training_days":REQUIRED_QLIB_DAILY_SESSIONS,
            "all_6_assets_have_sufficient_price_history_length":all_lengths,
            "approved_official_price_corporate_action_history":False,
            "total_return_not_issuer_verified":True,
            "qlib_forward_study_approved":False,
            "historical_price_study_only":True,
            "real_orders_enabled":False}


def main(argv=None):
    ap=argparse.ArgumentParser(description="Append-only isolated long stock history from public third-party vendors")
    mode=ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture",action="store_true")
    mode.add_argument("--audit",action="store_true")
    ap.add_argument("--db",default=DEFAULT_HISTORY)
    ap.add_argument("--source-db",default=DEFAULT_DB)
    args=ap.parse_args(argv)
    doc=capture_history(history_db=args.db,source_db=args.source_db) if args.capture else audit_history(args.db)
    print(json.dumps(doc,ensure_ascii=False))
    return 0 if doc["status"] in (
        "LONG_VENDOR_HISTORY_WITH_ISSUER_AND_TOTAL_RETURN_GATE_BLOCKED",
        "LONG_HISTORY_LENGTH_VERIFIED_NOT_MODEL_ELIGIBILITY"
    ) else 2

if __name__=="__main__":
    raise SystemExit(main())

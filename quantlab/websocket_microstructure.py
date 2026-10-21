"""Bounded public-only WebSocket quote + market-tape observation (NOT live execution).

One combined authenticated-NEVER public Upbit WSS subscription. Store received
frames, raw SHA256, arrival order and exchange times in a separate SQLite file.
Realtime trade-to-book links are valid only to an already RECEIVED, non-future,
fresh, actually persisted book. A displayed quote is NOT a customer fill.

Each process captures one bounded interval (10-60 seconds); outages between
intervals are explicit and historical WS depth cannot be backfilled. REST
observer remains the authoritative independent 10-minute coverage lane.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time
import uuid

from .continuous_portfolio import ASSETS
from .live_orderbook_evidence import _check_book
from .microstructure_observer import utcnow

PUBLIC_WSS="wss://api.upbit.com/websocket/v1"
MAX_BYTES=200_000
MAX_FRAMES=12000
MAX_BOOK_TRADE_LAG_MS=2000
MAX_CLOCK_FUTURE_MS=3000
MAX_TRADE_AGE_MS=12000
MIN_SECONDS=5
MAX_SECONDS=60
READ_TIMEOUT=2.5


def utc_ms():
    return int(time.time()*1000)


def subscription(ticket:str)->str:
    if not ticket or len(ticket)>90:
        raise ValueError("Invalid ticket")
    return json.dumps([
        {"ticket":ticket},
        {"type":"orderbook","codes":[s+".30" for s in ASSETS],
         "is_only_realtime":False},
        {"type":"trade","codes":list(ASSETS),"is_only_realtime":True},
        {"format":"DEFAULT"},
    ],separators=(",",":"),ensure_ascii=False)


def open_db(path:str|Path)->sqlite3.Connection:
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(p),timeout=12)
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, started_ms INTEGER NOT NULL,
        finished_ms INTEGER, status TEXT NOT NULL,
        intended_seconds INTEGER NOT NULL,
        frame_count INTEGER NOT NULL DEFAULT 0,
        rejected_frames INTEGER NOT NULL DEFAULT 0,
        duplicated_trade_ids INTEGER NOT NULL DEFAULT 0,
        reconnects INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT '',
        endpoint TEXT NOT NULL CHECK(endpoint='wss://api.upbit.com/websocket/v1'),
        mode TEXT NOT NULL CHECK(mode='PUBLIC_READ_ONLY')
    );
    CREATE TABLE IF NOT EXISTS frames (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        receipt_ms INTEGER NOT NULL, exchange_ms INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('orderbook','trade')),
        market TEXT NOT NULL,
        stream_type TEXT NOT NULL,
        trade_id TEXT,
        prev_book_seq INTEGER REFERENCES frames(seq),
        asof_exchange_lag_ms INTEGER,
        link_status TEXT NOT NULL,
        raw_sha256 TEXT NOT NULL, raw BLOB NOT NULL,
        UNIQUE(kind,market,trade_id)
    );
    CREATE INDEX IF NOT EXISTS frames_session ON frames(session_id,seq);
    CREATE INDEX IF NOT EXISTS frames_market ON frames(market,seq);
    """ )
    return c


def validate_frame(raw:bytes,now_ms:int)->dict:
    if not isinstance(raw,bytes) or not raw or len(raw)>MAX_BYTES:
        raise ValueError("Invalid WebSocket frame size")
    try: obj=json.loads(raw)
    except (ValueError,UnicodeError) as e:raise ValueError("Invalid JSON") from e
    if not isinstance(obj,dict):
        raise ValueError("Unexpected WebSocket payload")
    if "error" in obj:
        raise ValueError("Upbit WSS error response")
    typ=obj.get("type");market=obj.get("code")
    if typ not in ("orderbook","trade") or market not in ASSETS:
        raise ValueError("Unexpected market/type, refuse private data")
    stream=obj.get("stream_type")
    if stream not in ("SNAPSHOT","REALTIME"):
        raise ValueError("Missing exchange stream type")
    if typ=="orderbook":
        stamp=obj.get("timestamp")
        # Validates bid/ask sorting, 30-level shapes, tick-level=0.
        converted=dict(obj,market=market)
        _check_book(converted,datetime.fromtimestamp(now_ms/1000,timezone.utc))
        if len(obj["orderbook_units"])!=30:
            raise ValueError("Need complete unaggregated 30-level snapshot")
        identifier=None
    else:
        if stream!="REALTIME":
            raise ValueError("Trade snapshot not a newly streamed trade")
        stamp=obj.get("trade_timestamp")
        ident=obj.get("sequential_id")
        if type(ident) is not int or ident<=0:
            raise ValueError("Missing trade unique ID")
        identifier=str(ident)
        for k in ("trade_price","trade_volume"):
            v=obj.get(k)
            if type(v) not in (int,float) or not math.isfinite(v) or v<=0:
                raise ValueError("Invalid market print price/size")
        if obj.get("ask_bid") not in ("BID","ASK"):
            raise ValueError("Invalid aggressor side")
    if type(stamp) is not int or stamp<=1_500_000_000_000:
        raise ValueError("Invalid exchange timestamp")
    age=now_ms-stamp
    if age < -MAX_CLOCK_FUTURE_MS or age > MAX_TRADE_AGE_MS:
        raise ValueError("Stale/future exchange timestamp")
    return {"kind":typ,"market":market,"stream_type":stream,
            "exchange_ms":stamp,"trade_id":identifier,"raw":raw,
            "raw_sha256":sha256(raw).hexdigest(),"receipt_ms":now_ms}


def append_validated(c:sqlite3.Connection,session_id:str,frame:dict,
                     last_book:dict[str,tuple[int,int,int]],counts:dict)->dict:
    kind=frame["kind"];market=frame["market"]
    link=None;lag=None
    label="BOOK" if kind=="orderbook" else "NO_CAUSAL_BOOK"
    if kind=="trade":
        before=last_book.get(market)
        if before:
            seq,book_exchange,received=before
            lag=frame["exchange_ms"]-book_exchange
            # Both clocks must agree with the causal ordering.  A book
            # received later can NEVER be used as prior depth evidence.
            if (received<=frame["receipt_ms"] and
                0<=lag<=MAX_BOOK_TRADE_LAG_MS):
                link=seq
                label="CAUSAL_VISIBLE_L2_REFERENCE_ONLY"
            else:
                lag=None
    try:
        cursor=c.execute(
            """INSERT OR IGNORE INTO frames
              (session_id,receipt_ms,exchange_ms,kind,market,stream_type,
               trade_id,prev_book_seq,asof_exchange_lag_ms,link_status,
               raw_sha256,raw)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id,frame["receipt_ms"],frame["exchange_ms"],kind,market,
             frame["stream_type"],frame["trade_id"],link,lag,label,
             frame["raw_sha256"],frame["raw"]),
        )
    except sqlite3.IntegrityError as e:
        raise ValueError("Invalid causal frame/foreign key") from e
    if not cursor.rowcount:
        counts["duplicates"]+=1
        return {"status":"DUPLICATE_PUBLIC_TRADE","market":market}
    counts["frames"]+=1
    if kind=="orderbook":
        last_book[market]=(cursor.lastrowid,frame["exchange_ms"],frame["receipt_ms"])
        counts["books"]+=1
    else:
        counts["trades"]+=1
        if link is not None:counts["causal_trade_links"]+=1
        else:counts["unlinked_trades"]+=1
    return {"status":label,"market":market,"seq":cursor.lastrowid}


def _open_socket():
    import websocket  # optional extra [ws], no dependency on HTTP REST orders
    return websocket.create_connection(
        PUBLIC_WSS,timeout=6,enable_multithread=True,
        header=["User-Agent: FreeQuantLab-public-observer/0.2"],
    )


def capture(db:str|Path,*,duration:int=20,
            socket_factory=_open_socket,clock=utc_ms,
            mono=time.monotonic)->dict:
    if type(duration) is not int or not MIN_SECONDS<=duration<=MAX_SECONDS:
        raise ValueError("Capture must be bounded to 5-60 seconds")
    conn=open_db(db)
    started=clock()
    session_id=str(uuid.uuid4())
    counts={"frames":0,"books":0,"trades":0,"duplicates":0,
            "causal_trade_links":0,"unlinked_trades":0,"rejected":0,
            "reconnects":0}
    book_cache={}
    ws=None;status="INCOMPLETE";error=None
    with conn:
        conn.execute(
            "INSERT INTO sessions(id,started_ms,status,intended_seconds,endpoint,mode) VALUES(?,?,?,?,?,?)",
            (session_id,started,"RUNNING",duration,PUBLIC_WSS,"PUBLIC_READ_ONLY"))
    deadline=mono()+duration
    try:
        while mono()<deadline:
            if ws is None:
                ws=socket_factory()
                ws.settimeout(READ_TIMEOUT)
                ws.send(subscription(session_id))
                # Reconnect is a gap. Do not match pre-disconnect depth.
                book_cache.clear()
            try:
                packet=ws.recv()
            except Exception as exc:
                # websocket-client uses WebSocketTimeoutException, plus
                # socket.timeout. Others are bounded reconnectable failures.
                import socket
                try:
                    import websocket
                    expected=(socket.timeout,TimeoutError,websocket.WebSocketTimeoutException)
                except ImportError:
                    expected=(socket.timeout,TimeoutError)
                if isinstance(exc,expected):
                    continue
                counts["reconnects"]+=1
                ws.close();ws=None
                if counts["reconnects"]>=3:
                    raise ConnectionError("Maximum realtime reconnect attempts") from exc
                continue
            if packet=="PING" or packet==b"PING":
                ws.send("PONG")
                continue
            raw=packet if isinstance(packet,bytes) else str(packet).encode("utf-8")
            now=clock()
            try:
                frame=validate_frame(raw,now)
            except ValueError:
                counts["rejected"]+=1
                # A malformed item may be an error/unsupported protocol;
                # never report a clean continuous capture for it.
                if counts["rejected"]>20:
                    raise ValueError("Too many malformed WebSocket messages")
                continue
            with conn:
                append_validated(conn,session_id,frame,book_cache,counts)
            if counts["frames"]>=MAX_FRAMES:
                status="TRUNCATED_MAX_FRAMES"
                break
        else:
            status="BOUNDED_CAPTURE_COMPLETE"
        if not counts["books"] or not counts["trades"]:
            status="INSUFFICIENT_MARKET_TYPES"
        elif counts["reconnects"] or counts["rejected"]:
            status="CAPTURE_DEGRADED_GAPS_OR_BAD_MESSAGES"
    except Exception as exc:
        status="CAPTURE_FAILED"
        error=f"{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        if ws is not None:
            try:ws.close()
            except Exception:pass
        with conn:
            conn.execute(
                """UPDATE sessions SET finished_ms=?,status=?,frame_count=?,
                   rejected_frames=?,duplicated_trade_ids=?,reconnects=?,notes=?
                   WHERE id=?""",
                (clock(),status,counts["frames"],counts["rejected"],
                 counts["duplicates"],counts["reconnects"],error or "",session_id))
        conn.close()
    return {"session_id":session_id,"status":status,**counts,
            "failure":error,"read_only_public":True,
            "missing_between_sessions_not_backfilled":True,
            "actual_customer_order_fills":0}


def audit(db:str|Path,*,lookback_hours:int|None=None)->dict:
    if lookback_hours is not None and (type(lookback_hours) is not int or not 1<=lookback_hours<=720):
        raise ValueError('Invalid audit lookback hours')
    cutoff=(utc_ms()-lookback_hours*3600*1000) if lookback_hours is not None else 0
    # Read-only open: an empty/missing database must not be created by an audit.
    uri="file:"+str(Path(db).resolve())+"?mode=ro"
    with sqlite3.connect(uri,uri=True) as c:
        if c.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise ValueError("SQLite integrity error")
        sessions=c.execute(
            "SELECT id,status,frame_count,rejected_frames,reconnects,finished_ms FROM sessions WHERE started_ms>=? ORDER BY started_ms",(cutoff,)
        ).fetchall()
        records=c.execute(
            """SELECT seq,session_id,receipt_ms,exchange_ms,kind,market,
               stream_type,trade_id,prev_book_seq,asof_exchange_lag_ms,
               link_status,raw_sha256,raw
               FROM frames WHERE session_id IN (SELECT id FROM sessions WHERE started_ms>=?) ORDER BY seq""",(cutoff,)
        ).fetchall()
        seen=set()
        for seq,sess,rx,ts,kind,market,stream,tid,ref,lag,label,h,raw in records:
            if sha256(raw).hexdigest()!=h:
                raise ValueError("Tampered WebSocket raw SHA")
            obj=validate_frame(raw,rx)
            if (obj["kind"],obj["market"],obj["exchange_ms"],
                obj["stream_type"],obj["trade_id"])!=(kind,market,ts,stream,tid):
                raise ValueError("Stored WebSocket metadata mismatch")
            if ref is not None:
                earlier=c.execute(
                    "SELECT seq,session_id,kind,market,receipt_ms,exchange_ms FROM frames WHERE seq=?",
                    (ref,)).fetchone()
                if (earlier is None or earlier[0]>=seq or earlier[1]!=sess or
                    earlier[2]!="orderbook" or earlier[3]!=market or
                    earlier[4]>rx or not 0<=ts-earlier[5]<=MAX_BOOK_TRADE_LAG_MS or
                    lag!=ts-earlier[5] or label!="CAUSAL_VISIBLE_L2_REFERENCE_ONLY"):
                    raise ValueError("Invalid lookahead/foreign book link")
            elif kind=="trade" and label!="NO_CAUSAL_BOOK":
                raise ValueError("Forged matched trade")
            if kind=="trade":
                key=(market,tid)
                if key in seen:raise ValueError("Repeated trade unique ID")
                seen.add(key)
        if sum(x[2] for x in sessions)!=len(records):
            raise ValueError("Session frame count cannot reconcile")
        linked_lags=[lag for row in records for lag in [row[9]]
                     if row[10]=="CAUSAL_VISIBLE_L2_REFERENCE_ONLY"]
        latest_finish=max((s[5] for s in sessions if s[5] is not None and s[1]=="BOUNDED_CAPTURE_COMPLETE"),default=None)
        recent_age=(utc_ms()-latest_finish)/1000 if latest_finish else None
        return {
            "status":"PUBLIC_WS_EVIDENCE_AUDITED_NOT_REAL_ORDER_FILLS",
            "detailed_lookback_hours":lookback_hours,
            "seconds_since_last_clean_session":recent_age,
            "sessions":len(sessions),
            "complete_sessions":sum(x[1]=="BOUNDED_CAPTURE_COMPLETE" for x in sessions),
            "degraded_or_failed_sessions":sum(x[1]!="BOUNDED_CAPTURE_COMPLETE" for x in sessions),
            "book_frames":sum(x[4]=="orderbook" for x in records),
            "market_trade_frames":sum(x[4]=="trade" for x in records),
            "trade_to_prior_book_links":len(linked_lags),
            "linked_book_exchange_lag_ms_median":statistics.median(linked_lags) if linked_lags else None,
            "linked_book_exchange_lag_ms_max":max(linked_lags) if linked_lags else None,
            "unmatched_trade_frames":sum(x[4]=="trade" and x[8] is None for x in records),
            "max_capture_gap_unverified":True,
            "raw_sha256_integrity":"PASS",
            "still_no_verified_own_fills":True,
            "real_orders_enabled":False,
            "automatic_promotion":False,
        }


def main(argv=None)->int:
    p=argparse.ArgumentParser(description="Read-only bounded Upbit public WS observer")
    p.add_argument("--db",default="/home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3")
    p.add_argument("--seconds",type=int,default=20)
    p.add_argument("--audit",action="store_true")
    p.add_argument("--audit-hours",type=int,default=48)
    p.add_argument("--require-recent-seconds",type=int,default=None)
    args=p.parse_args(argv)
    result=audit(args.db,lookback_hours=args.audit_hours) if args.audit else capture(args.db,duration=args.seconds)
    print(json.dumps(result,ensure_ascii=False))
    if result.get("status") in ("CAPTURE_FAILED","INSUFFICIENT_MARKET_TYPES","TRUNCATED_MAX_FRAMES"):
        return 2
    if args.audit and args.require_recent_seconds is not None:
        if args.require_recent_seconds<=0:raise ValueError("Invalid freshness threshold")
        age=result["seconds_since_last_clean_session"]
        if age is None or age<0 or age>args.require_recent_seconds:
            return 2
    return 0

if __name__=="__main__":
    raise SystemExit(main())

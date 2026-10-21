"""Immutable Upbit OHLCV archive that catches up after Windows/WSL downtime.

Real exchange public historical daily candles are recoverable. A ticker from
a missed moment and its hypothetical broker/paper execution are NOT.
No access to prospective virtual account DBs and no invented fills.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import sys

import pandas as pd

from .data import upbit_daily
from .quality import validate_shadow_market

ASSETS=("KRW-BTC","KRW-ETH")
HISTORY_BARS=800
DB_BASENAME="market_history.db"


class ArchiveError(ValueError):
    pass


@contextmanager
def _exclusive_archive(path: str | Path):
    lock=Path(str(path)+".lock")
    lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open("a+b") as handle:
        os.fchmod(handle.fileno(),0o600)
        try:
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveError("Market archive refresh already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def _utc(now: datetime | None)->datetime:
    instant=now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ArchiveError("Timezone-aware current timestamp required")
    return instant.astimezone(timezone.utc)


def _guard_path(db: str | Path)->Path:
    p=Path(db).resolve()
    if p.name!=DB_BASENAME:
        raise ArchiveError("Archive must not point at any virtual trading ledger")
    return p


def _prepare(con: sqlite3.Connection):
    con.execute("""CREATE TABLE IF NOT EXISTS archive_candles (
      symbol TEXT NOT NULL,utc_day TEXT NOT NULL,
      open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,
      close REAL NOT NULL,volume REAL NOT NULL,
      first_retrieved_at_utc TEXT NOT NULL,
      provenance TEXT NOT NULL CHECK(provenance IN ('historical_seed','historical_catchup')),
      PRIMARY KEY(symbol,utc_day)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS archive_runs (
      run_id INTEGER PRIMARY KEY AUTOINCREMENT,
      attempted_at_utc TEXT NOT NULL,status TEXT NOT NULL,
      details_json TEXT NOT NULL
    )""")


def _fetch(symbol: str, stamp: datetime, *, fetch_fn):
    frame=fetch_fn(symbol,HISTORY_BARS,now=stamp)
    info=validate_shadow_market(frame,stamp)
    if len(frame)<100 or not pd.to_datetime(frame["date"]).is_monotonic_increasing:
        raise ArchiveError("Invalid historical candle order/coverage")
    if frame["date"].duplicated().any():
        raise ArchiveError("Historical source returned duplicate dates")
    days=pd.to_datetime(frame["date"]).dt.date
    if (days.diff().iloc[1:].dt.days!=1).any():
        raise ArchiveError("Historical candles are not continuous")
    if days.iloc[-1]!=(stamp.date()-timedelta(days=1)):
        raise ArchiveError("Historical source has not completed yesterday's UTC candle")
    return frame,info


def _equal(old: sqlite3.Row,values: tuple[float,...])->bool:
    return all(math.isclose(float(old[key]),val,rel_tol=1e-10,abs_tol=1e-7)
               for key,val in zip(("open","high","low","close","volume"),values))


def _append_one(con: sqlite3.Connection,symbol: str,frame: pd.DataFrame,
                retrieved_at: datetime)->dict:
    old=con.execute(
        "SELECT * FROM archive_candles WHERE symbol=? ORDER BY utc_day",
        (symbol,),
    ).fetchall()
    by_day={row["utc_day"]:row for row in old}
    source_days=[pd.Timestamp(day).date().isoformat() for day in frame["date"]]
    end=source_days[-1]
    if old:
        last=old[-1]["utc_day"]
        # An offline interval beyond the retained Upbit request horizon
        # cannot be reconstructed safely; do not hide the lost gap.
        if last not in set(source_days) and last<source_days[0]:
            raise ArchiveError("Data provider window no longer overlaps archived "
                               "history; manual historical resynchronization required")
        if last>end:
            raise ArchiveError("Historical exchange source is older than local archive")
    else:
        last=None
    additions=[]
    revisions=[]
    for item,day in zip(frame.itertuples(index=False),source_days):
        numeric=(float(item.open),float(item.high),float(item.low),
                 float(item.close),float(item.volume))
        if not all(math.isfinite(v) for v in numeric):
            raise ArchiveError("Nonfinite source historical OHLCV")
        if day in by_day:
            if not _equal(by_day[day],numeric):
                revisions.append(day)
        elif last is None or day>last:
            additions.append((symbol,day,*numeric,retrieved_at.isoformat(),
                              "historical_seed" if last is None else "historical_catchup"))
        else:
            raise ArchiveError("An archived historical date was removed: "
                               "manual provider data review needed")
    if revisions:
        raise ArchiveError(
            f"Upbit historical OHLCV changed for {symbol} "
            f"({len(revisions)} overlapping UTC days); immutable archive not rewritten"
        )
    if old and additions:
        if additions[0][1]!=(datetime.fromisoformat(last).date()+timedelta(days=1)).isoformat():
            raise ArchiveError("Historical catch-up is missing intervening daily candles")
    con.executemany(
        "INSERT INTO archive_candles "
        "(symbol,utc_day,open,high,low,close,volume,first_retrieved_at_utc,provenance) "
        "VALUES(?,?,?,?,?,?,?,?,?)",additions,
    )
    return {
        "symbol":symbol,"status":"PASS",
        "archive_rows_before":len(old),"new_historical_candles":len(additions),
        "archive_last_utc_day":end,
        "oldest_recovered_utc_day":additions[0][1] if additions else None,
        "newest_recovered_utc_day":additions[-1][1] if additions else None,
        "does_not_recreate_paper_orders":True,
    }


def catch_up(db: str | Path="state/market_history.db", *,
             now: datetime|None=None, fetch_fn=upbit_daily)->dict:
    stamp=_utc(now)
    path=_guard_path(db)
    path.parent.mkdir(parents=True,exist_ok=True)
    results=[]
    problems=[]
    with _exclusive_archive(path):
        with sqlite3.connect(path,timeout=15) as con:
            con.row_factory=sqlite3.Row
            con.execute("PRAGMA busy_timeout=15000")
            con.execute("PRAGMA journal_mode=WAL")
            _prepare(con)
            # Fetch first, validate and then persist each asset atomically.
            # Failure in one market won't poison the other market.
            for symbol in ASSETS:
                try:
                    frame,_=_fetch(symbol,stamp,fetch_fn=fetch_fn)
                    with con:
                        result=_append_one(con,symbol,frame,stamp)
                    results.append(result)
                except Exception as exc:
                    problems.append({"symbol":symbol,"reason":type(exc).__name__,
                                     "explanation":str(exc)[:190]})
            overall="PASS" if not problems else "FAIL"
            info={
                "status":overall,"attempted_at_utc":stamp.isoformat(),
                "market_data_source":"Upbit_public_historical_UTC_OHLCV",
                "candle_interval":"1d","assets":results,"issues":problems,
                "historical_trade_execution_observed":False,
                "missing_paper_orders_reconstructed":False,
                "real_orders_enabled":False,
            }
            with con:
                con.execute(
                    "INSERT INTO archive_runs(attempted_at_utc,status,details_json) "
                    "VALUES(?,?,?)",
                    (stamp.isoformat(),overall,json.dumps(info,sort_keys=True)),
                )
            return info


def audit_archive(db: str | Path="state/market_history.db",
                  *,now: datetime|None=None)->dict:
    stamp=_utc(now)
    path=_guard_path(db)
    if not path.is_file():
        return {"status":"NOT_INITIALIZED","rows":0,
                "real_orders_enabled":False}
    con=sqlite3.connect(f"file:{path}?mode=ro",uri=True)
    con.row_factory=sqlite3.Row
    try:
        latest=con.execute(
            "SELECT status,attempted_at_utc,details_json FROM archive_runs "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        rows=con.execute(
            "SELECT symbol,utc_day,open,high,low,close,volume,provenance "
            "FROM archive_candles ORDER BY symbol,utc_day"
        ).fetchall()
    finally:
        con.close()
    counters={}
    issues=[]
    for item in rows:
        counters.setdefault(item["symbol"],[]).append(item)
    wanted=(stamp.date()-timedelta(days=1)).isoformat()
    per_symbol=[]
    for symbol in ASSETS:
        data=counters.get(symbol,[])
        dates=[r["utc_day"] for r in data]
        if not dates:
            issues.append(symbol+":NO_SAVED_HISTORICAL_CANDLES")
        if any((pd.Timestamp(b)-pd.Timestamp(a)).days!=1
               for a,b in zip(dates,dates[1:])):
            issues.append(symbol+":DISCONTINUOUS_ARCHIVE")
        last=dates[-1] if dates else None
        if last is not None and last<wanted:
            issues.append(symbol+":ARCHIVE_NOT_CURRENT")
        per_symbol.append({"symbol":symbol,"rows":len(dates),
                           "latest_utc_day":last,
                           "historical_catchup_rows":sum(
                               r["provenance"]=="historical_catchup" for r in data),
                           "expected_latest_utc_day":wanted})
    if latest is not None and latest["status"]!="PASS":
        issues.append("LAST_CATCHUP_ATTEMPT_FAILED")
    return {
        "status":"PASS" if not issues else "REVIEW","checked_at_utc":stamp.isoformat(),
        "source":"Upbit_public_daily_historical_candles",
        "rows":len(rows),"assets":per_symbol,"issues":issues,
        "last_catchup_attempt_at_utc":latest["attempted_at_utc"] if latest else None,
        "last_catchup_status":latest["status"] if latest else None,
        "historical_trade_execution_observed":False,
        "real_orders_enabled":False,
    }


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="Immutable public UTC daily market archive; no virtual trade backfill")
    parser.add_argument("action",choices=("catch-up","status"))
    parser.add_argument("--db",default="state/market_history.db")
    args=parser.parse_args(argv)
    try:
        result=(catch_up(args.db) if args.action=="catch-up" else audit_archive(args.db))
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result["status"] in ("FAIL","REVIEW") else 0
    except (OSError,ValueError,sqlite3.Error) as exc:
        print("MARKET ARCHIVE FAILED CLOSED: "
              +type(exc).__name__+": "+str(exc)[:200],file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

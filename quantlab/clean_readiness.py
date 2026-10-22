"""Read-only market and frozen-cohort preflight. Never records trades or approvals."""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import date,datetime,time,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Callable
from .clean_cohort import inspect_clean
from .data import DataError,fetch,live_paper_quote
from .monitor import _read_db
from .prospective import _utc,read_protocol
from .protocol_compliance import verify_frozen_accounts
from .quality import validate_public_quote,validate_shadow_market
from .shadow import _completed_utc_day

DEFAULT_DB="/home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db"
DEFAULT_PROTOCOL=("/home/hong/apps/FreeQuantLab-clean-runtime/"
                  "protocols/2026-10-21-btc-eth-clean-90d.json")
SYMBOLS=("KRW-BTC","KRW-ETH")

class ReadinessError(RuntimeError):
    pass

def inspect_readiness(db:str|Path=DEFAULT_DB,protocol:str|Path=DEFAULT_PROTOCOL,*,
                      now:datetime|None=None,online:bool=False,
                      fetch_fn:Callable=fetch,quote_fn:Callable=live_paper_quote)->dict:
    current=_utc(now)
    plan=read_protocol(protocol)
    first=date.fromisoformat(plan["first_eligible_decision_utc"])
    opens=datetime.combine(first+timedelta(days=1),time(0,10),timezone.utc)
    if current>=opens:
        raise ReadinessError("Preflight expired: use actual Watchdog evidence")
    if current<datetime.fromisoformat(plan["created_at_utc"]):
        raise ReadinessError("System clock predates preregistration")
    if (plan["required_complete_utc_days"]!=90 or
        plan["real_broker_order_permission"] is not False or
        plan["automatic_strategy_promotion"] is not False):
        raise ReadinessError("Unexpected clean cohort protocol")
    with closing(_read_db(db)) as con:
        con.execute("BEGIN")
        try:
            accounts=con.execute("SELECT * FROM shadow_accounts ORDER BY symbol,label").fetchall()
            events=con.execute("SELECT count(*) FROM shadow_events").fetchone()[0]
            quotes=con.execute("SELECT count(*) FROM shadow_quote_evidence").fetchone()[0]
            verify_frozen_accounts(accounts,protocol,now=current)
        finally:
            con.execute("ROLLBACK")
    if (len(accounts)!=6 or events!=0 or quotes!=0 or
        sorted({a["symbol"] for a in accounts})!=list(SYMBOLS) or
        len({a["track_id"] for a in accounts})!=6):
        raise ReadinessError("Contaminated or unexpected frozen cohort")
    audit=inspect_clean(db,protocol,now=current)
    if (audit["status"]!="WAITING_FIRST_DUE" or audit["issues"] or
        audit["expected_due_account_days"]!=0 or
        audit["recorded_due_account_days"]!=0 or
        audit["ledger_audit_status"]!="PASS"):
        raise ReadinessError("Independent clean cohort preflight failed")
    probes=[]
    if online:
        for symbol in SYMBOLS:
            asset={"provider":"upbit","symbol":symbol,"kind":"crypto",
                   "currency":"KRW","bars":240}
            frame=fetch_fn(asset)
            day=_completed_utc_day(frame,current)
            quality=validate_shadow_market(frame,current)
            price,session=quote_fn("upbit",symbol,now=current)
            validate_public_quote(quality["last_completed_close"],price)
            if session!=current.date().isoformat():
                raise ReadinessError("Public ticker session mismatch")
            probes.append({"symbol":symbol,"completed_utc_day":day,
                           "bars_checked":len(frame),"ticker_validated":True})
    return {"status":"READY_MARKET_VALIDATED" if online else "READY_LEDGER_ONLY",
            "checked_at_utc":current.isoformat(),
            "first_decision_utc":first.isoformat(),"opens_utc":opens.isoformat(),
            "protocol_sha256":plan["sha256"],"frozen_accounts":6,
            "simulated_events":0,"quote_evidence_rows":0,
            "public_market_probes":probes,"online_market_checked":online,
            "first_day_accepted":False,"receipt_created":False,
            "real_orders_enabled":False,"automatic_strategy_promotion":False}

def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="Read-only frozen clean market preflight")
    p.add_argument("--db",default=DEFAULT_DB)
    p.add_argument("--protocol",default=DEFAULT_PROTOCOL)
    p.add_argument("--online",action="store_true")
    a=p.parse_args(argv)
    try:
        print(json.dumps(inspect_readiness(a.db,a.protocol,online=a.online),
                         ensure_ascii=False,sort_keys=True))
        return 0
    except (ReadinessError,DataError,ValueError,KeyError,TypeError,
            sqlite3.Error,OSError,RuntimeError) as exc:
        print(json.dumps({"status":"READINESS_BLOCKED","cause":type(exc).__name__,
                          "no_virtual_events_created":True,"real_orders":0}),file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())

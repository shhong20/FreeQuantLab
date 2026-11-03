"""Pre-open non-trading WSL readiness audit for the frozen clean forward cohort.

This checks systemd calendar timers and Upbit's PUBLIC ticker endpoint. It
never executes the decision runner, writes a brokerage order or changes the
frozen ledger. An offline Windows machine cannot execute this audit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import subprocess
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .ledger_audit import audit_shadow_ledger
from .monitor import _read_db
from .prospective import _utc, read_protocol
from .protocol_compliance import verify_frozen_accounts
from .quote_quality import execution_proxy_quality

KST=ZoneInfo("Asia/Seoul")
TIMER_UNITS=(
    "freequant-shadow.timer",
    "freequant-clean-shadow.timer",
    "freequant-exaone.timer",
    "freequant-qlib-lab.timer",
    "freequant-clean-watchdog.timer",
)
PUBLIC_URL="https://api.upbit.com/v1/ticker?markets=KRW-BTC%2CKRW-ETH"
SCHEMA="CLEAN_90D_PREOPEN_READINESS_V1_20261103"
CUTOFF=(10,20)
OBSERVATION_BEGINS=(9,0)
START_TIME=(8,30)


def _systemd_timer(unit:str)->dict:
    if unit not in TIMER_UNITS:
        raise ValueError("Unexpected systemd user unit")
    # Noninteractive WSL and Windows-scheduled launches need the same
    # per-user systemd bus address as interactive user-manager sessions.
    runtime=f"/run/user/{os.getuid()}"
    env=dict(os.environ)
    env["XDG_RUNTIME_DIR"]=runtime
    env["DBUS_SESSION_BUS_ADDRESS"]=f"unix:path={runtime}/bus"
    result=subprocess.run(
        ["systemctl","--user","show",unit,"--property=ActiveState",
         "--property=UnitFileState","--property=NextElapseUSecRealtime"],
        capture_output=True,text=True,timeout=5,check=False,env=env)
    props=dict(line.split("=",1) for line in result.stdout.splitlines()
               if "=" in line)
    enabled=props.get("UnitFileState")=="enabled"
    active=props.get("ActiveState")=="active"
    next_event=props.get("NextElapseUSecRealtime","")
    return {"unit":unit,"registered":result.returncode==0,
            "enabled":enabled,"active":active,
            "next_calendar_time":next_event,
            "calendar_timer_ready":bool(result.returncode==0 and active and
                                       enabled and next_event not in ("","n/a"))}


def _public_ticker()->dict:
    """Public GET only; never store live quote prices or credentials."""
    req=Request(PUBLIC_URL,headers={
        "User-Agent":"FreeQuantLab-Frozen-Cohort-Readiness/1.0",
        "Accept":"application/json"})
    with urlopen(req,timeout=5) as response:
        if response.status!=200:
            raise ValueError("Public market ticker not HTTP 200")
        raw=response.read(16001)
    if len(raw)>16000:
        raise ValueError("Unexpected Upbit public market response size")
    values=json.loads(raw)
    if (not isinstance(values,list) or len(values)!=2 or
        {v.get("market") for v in values if isinstance(v,dict)}
        !={"KRW-BTC","KRW-ETH"} or
        any(type(v.get("trade_price")) not in (float,int) or
            not math.isfinite(v["trade_price"]) or v["trade_price"]<=0
            for v in values)):
        raise ValueError("Incomplete public Upbit ticker response")
    return {
        "source":"Upbit public GET /v1/ticker (no auth)",
        "expected_markets":["KRW-BTC","KRW-ETH"],
        "public_ticker_reachable":True,
        "live_prices_not_persisted":True,
    }


def inspect(*,db:Path|str,protocol:Path|str,now:datetime|None=None,
            timer_probe=None,ticker_probe=None)->dict:
    current=_utc(now)
    plan=read_protocol(protocol)
    if (plan["required_complete_utc_days"]!=90 or
        len(plan["account_set"])!=6 or
        plan["real_broker_order_permission"] is not False):
        raise ValueError("Frozen 90-day clean cohort identity changed")
    if (audit_shadow_ledger(db,now=current)["status"]!="PASS" or
        execution_proxy_quality(db)["status"] not in
            ("PASS","AWAITING_FIRST_OBSERVATION")):
        raise ValueError("Failed independent clean ledger or quote evidence")
    with _read_db(db) as con:
        con.execute("BEGIN")
        accounts=con.execute("SELECT * FROM shadow_accounts ORDER BY track_id").fetchall()
        con.execute("COMMIT")
    verify_frozen_accounts(accounts,protocol,now=current)
    if timer_probe is None:
        timer_probe=_systemd_timer
    if ticker_probe is None:
        ticker_probe=_public_ticker
    timers=[timer_probe(unit) for unit in TIMER_UNITS]
    if len(timers)!=len(TIMER_UNITS) or any(
        t.get("unit")!=unit for t,unit in zip(timers,TIMER_UNITS)):
        raise ValueError("User timer audit response mismatch")
    # Never convert an outbound API failure into a market-data observation.
    try:
        public=ticker_probe()
        market_ok=public.get("public_ticker_reachable") is True and (
            public.get("expected_markets")==["KRW-BTC","KRW-ETH"])
    except (OSError,ValueError,TimeoutError,KeyError):
        public={"source":"Upbit public GET /v1/ticker (no auth)",
                "expected_markets":["KRW-BTC","KRW-ETH"],
                "public_ticker_reachable":False,
                "live_prices_not_persisted":True}
        market_ok=False
    local=current.astimezone(KST)
    time_tuple=(local.hour,local.minute)
    if time_tuple>=CUTOFF:
        window="AFTER_OBSERVATION_WINDOW"
    elif time_tuple<START_TIME:
        window="BEFORE_PREOPEN_CHECK_WINDOW"
    elif time_tuple<OBSERVATION_BEGINS:
        window="PREOPEN"
    else:
        window="OBSERVATION_WINDOW"
    timers_ok=all(x.get("calendar_timer_ready") is True for x in timers)
    infra_ready=timers_ok and market_ok
    current_status=(
        "INFRA_READINESS_FAILED_NO_AUTOMATIC_ORDERS"
        if not infra_ready else
        "READY_FOR_FUTURE_OBSERVATION_ONLY"
        if window in ("PREOPEN","OBSERVATION_WINDOW") else
        "WINDOW_CLOSED_NO_HISTORICAL_REPLAY"
        if window=="AFTER_OBSERVATION_WINDOW" else
        "INFRA_READY_EARLY_NO_DECISION_YET"
    )
    return {
        "schema":SCHEMA,
        "status":current_status,
        "checked_at_utc":current.isoformat(),
        "kst_date":local.date().isoformat(),
        "kst_clock":local.strftime("%H:%M"),
        "observation_window_status":window,
        "clean_90d_protocol_sha256":plan["sha256"],
        "frozen_accounts_verified":len(accounts),
        "frozen_ledger_audit":"PASS",
        "frozen_quote_evidence_audit":"PASS_OR_WAITING_FIRST",
        "calendar_timers":timers,
        "calendar_timers_ready":timers_ok,
        "public_market_probe":public,
        "public_market_reachable":market_ok,
        "infrastructure_ready":infra_ready,
        "windows_powered_on_guaranteed":False,
        "wsl_running_when_windows_off_guaranteed":False,
        "future_09_24_observation_guaranteed":False,
        "missed_days_backfilled":0,
        "past_virtual_orders_created":0,
        "live_orders":0,
        "strategy_returns":None,
        "real_trading_enabled":False,
        "strategy_promotion_enabled":False,
    }


def _save_receipt(report:dict,folder:Path)->Path:
    folder=folder.expanduser().resolve()
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    folder.chmod(0o700)
    dt=datetime.fromisoformat(report["checked_at_utc"])
    stamp=dt.strftime("%Y%m%dT%H%M%S%fZ")
    dest=folder/("readiness-"+stamp+".json")
    body=(json.dumps(report,sort_keys=True,ensure_ascii=False,
                     allow_nan=False,indent=2)+"\n").encode()
    fd=os.open(dest,os.O_WRONLY|os.O_EXCL|os.O_CREAT,0o600)
    with os.fdopen(fd,"wb") as out:
        out.write(body)
        out.flush()
        os.fsync(out.fileno())
    pin=dest.with_suffix(".json.sha256")
    fd=os.open(pin,os.O_WRONLY|os.O_EXCL|os.O_CREAT,0o600)
    with os.fdopen(fd,"w") as out:
        out.write(sha256(body).hexdigest()+"\n")
    return dest


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description="Read-only preopen frozen 90d readiness")
    parser.add_argument("--db",required=True)
    parser.add_argument("--protocol",required=True)
    parser.add_argument("--report-dir",required=True)
    args=parser.parse_args(argv)
    try:
        report=inspect(db=args.db,protocol=args.protocol)
        receipt=_save_receipt(report,Path(args.report_dir))
        print(json.dumps({"status":report["status"],
                          "window":report["observation_window_status"],
                          "timers_ready":report["calendar_timers_ready"],
                          "public_market_reachable":report["public_market_reachable"],
                          "receipt":str(receipt),
                          "no_historical_fills":True,
                          "real_orders":0},
                         sort_keys=True))
        return 0 if report["infrastructure_ready"] else 2
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError):
        print(json.dumps({"status":"PREOPEN_READINESS_AUDIT_FAILED_CLOSED",
                          "real_orders":0,"past_fills":0}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

"""One-day, research-only KRX market-data pilot with idempotent captures.

The 2026-10-29 pilot samples live (not historical) 10-level quotes, recent
exchange trades and 1m candles for Samsung and SK hynix. It NEVER sends orders,
never estimates fill probabilities, never rewrites original research files.
Scheduling requires an already-running WSL cron daemon; WSL suspension means
the job can be missed and cannot be reconstructed as a synchronized snapshot.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
from zoneinfo import ZoneInfo

import exchange_calendars as xc

from .data import _kis_token
from .intraday_krx_halt_position_stress import _read_verified
from .intraday_krx_joint_evidence import collect, compare, SYMBOLS
from .intraday_kis_venues import VenueProbeError

KST=ZoneInfo("Asia/Seoul")
PILOT_SESSION="2026-10-29"
WINDOWS={
    "morning":(9*60+30,10*60+10),
    "midday":(12*60+15,13*60+15),
    "afternoon":(14*60+10,15*60),
}
DEFAULT_ROOT=Path.home()/"apps"/"FreeQuantLab"/"state"/"intraday_research"/"krx_joint_pilot_20261029_r1"
SAMPLES_PER_STOCK=2


def preflight(*,session:str,window:str,now:datetime,check_now:bool=True)->dict:
    if session!=PILOT_SESSION or window not in WINDOWS:
        raise VenueProbeError("Only the explicitly approved 2026-10-29 KRX pilot is allowed")
    if not xc.get_calendar("XKRX").is_session(session):
        raise VenueProbeError("Pilot date is not an XKRX trading session")
    if now.tzinfo is None or now.utcoffset() is None:
        raise VenueProbeError("Timezone-aware pilot clock required")
    local=now.astimezone(KST)
    minute=local.hour*60+local.minute
    start,stop=WINDOWS[window]
    if check_now and (
        local.strftime("%Y-%m-%d")!=session or
        not start<=minute<stop
    ):
        raise VenueProbeError("Not the requested pilot market session/window")
    return {"session":session,"window":window,"KRX_calendar_session":True,
            "market_sampling_window_kst":f"{start//60:02d}:{start%60:02d}-{stop//60:02d}:{stop%60:02d}",
            "expected_market_http_gets":len(SYMBOLS)*3*SAMPLES_PER_STOCK,
            "orders_enabled":False}


def _get_result(root:Path,session:str,window:str)->dict:
    from hashlib import sha256
    capture=root/f"{session}_{window}_raw"
    audited=root/f"{session}_{window}_audit"
    if audited.exists():
        data=_read_verified(audited,"joint_comparison.json")
        provenance=_read_verified(capture,"manifest.json")
        if (data.get("status")!="KRX_NEAR_TIME_THREE_SOURCE_CONSISTENCY_NOT_FILL_VALIDATION" or
            provenance.get("status")!="READ_ONLY_KRX_J_NEAR_TIME_L2_TICKS_1M_NO_FILLS" or
            data.get("broker_orders")!=0 or
            data.get("manifest_sha256")!=sha256((capture/"manifest.json").read_bytes()).hexdigest()):
            raise VenueProbeError("Existing pilot audit cannot be trusted")
        return {"status":"ALREADY_AUDITED","session":session,"window":window,
                "sample_triplets":data["sample_triplets"],
                "same_minute_ticks":data["matched_tick_minute_row_count"],
                "broker_orders":0}
    if capture.exists() and not (capture/"manifest.json").exists():
        raise VenueProbeError("Incomplete original KRX capture must be isolated")
    if capture.exists():
        _read_verified(capture,"manifest.json")
    return {"status":"NOT_YET_AUDITED","session":session,"window":window,
            "capture_exists":capture.exists(),"broker_orders":0}


def execute(*,session:str,window:str,
            root:Path|str=DEFAULT_ROOT,
            clock=lambda:datetime.now(timezone.utc),
            token_supplier=_kis_token,
            collector=collect,
            auditor=compare)->dict:
    info=preflight(session=session,window=window,now=clock())
    dest=Path(root).expanduser().resolve()
    dest.mkdir(parents=True,exist_ok=True,mode=0o700)
    if dest.is_symlink() or (dest.stat().st_mode & 0o022):
        raise VenueProbeError("Unsafe writable pilot output directory")
    lock=dest/".pilot.lock"
    fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077:
            raise VenueProbeError("Unsafe pilot lock")
        fcntl.flock(fd,fcntl.LOCK_EX)
        prior=_get_result(dest,session,window)
        if prior["status"]=="ALREADY_AUDITED":
            return prior
        source=dest/f"{session}_{window}_raw"
        audited=dest/f"{session}_{window}_audit"
        if not prior["capture_exists"]:
            # No token issuance before all session/window and output checks.
            token=token_supplier()
            if not isinstance(token,str) or not token.strip():
                raise VenueProbeError("No reusable KIS authentication available")
            collector(out_dir=source,key=os.getenv("KIS_APP_KEY",""),
                      secret=os.getenv("KIS_APP_SECRET",""),token=token,
                      count=SAMPLES_PER_STOCK)
        # If a source exists from a prior run, re-audit without re-capturing.
        value=auditor(source_dir=source,out_dir=audited)
        if (value.get("status")!="KRX_NEAR_TIME_THREE_SOURCE_CONSISTENCY_NOT_FILL_VALIDATION" or
            value.get("broker_orders")!=0):
            raise VenueProbeError("Pilot consistency proof not established")
        return {"status":"AUDITED_RESEARCH_ONLY","session":session,"window":window,
                "sample_triplets":value["sample_triplets"],
                "same_minute_ticks":value["matched_tick_minute_row_count"],
                "broker_orders":0,
                "strategy_returns_verified":False}
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def _load_local_kis_credentials() -> None:
    """Load only two KIS names from a private 0600 file; never execute shell."""
    if os.getenv("KIS_APP_KEY") and os.getenv("KIS_APP_SECRET"):
        return
    private=Path.home()/".config"/"freequantlab"/"kis.env"
    if private.is_symlink():
        raise VenueProbeError("Symlinked KIS credentials are refused")
    details=private.stat()
    if (not stat.S_ISREG(details.st_mode) or
        details.st_uid!=os.getuid() or details.st_mode&0o077):
        raise VenueProbeError("KIS credentials must be an owner-only file")
    content=private.read_text(encoding="utf-8")
    if len(content)>8192:
        raise VenueProbeError("Oversized private KIS configuration")
    allowed={}
    for line in content.splitlines():
        line=line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise VenueProbeError("Invalid private KIS configuration")
        key,value=line.split("=",1)
        if key not in ("KIS_APP_KEY","KIS_APP_SECRET"):
            raise VenueProbeError("Unexpected private KIS configuration key")
        value=value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in ("'",'"'):
            value=value[1:-1]
        if not value or len(value)>1024:
            raise VenueProbeError("Invalid KIS configuration value")
        allowed[key]=value
    if set(allowed)!={"KIS_APP_KEY","KIS_APP_SECRET"}:
        raise VenueProbeError("Private KIS credentials not configured")
    for key,value in allowed.items():
        os.environ.setdefault(key,value)


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="One-day 2026-10-29 KRX J research-only sampling")
    p.add_argument("--session",required=True)
    p.add_argument("--window",required=True,choices=tuple(WINDOWS))
    p.add_argument("--root",default=str(DEFAULT_ROOT))
    modes=p.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight",action="store_true")
    modes.add_argument("--execute",action="store_true")
    modes.add_argument("--status",action="store_true")
    args=p.parse_args(argv)
    try:
        if args.execute:
            preflight(session=args.session,window=args.window,
                      now=datetime.now(timezone.utc))
            _load_local_kis_credentials()
            result=execute(session=args.session,window=args.window,root=args.root)
        else:
            check=preflight(session=args.session,window=args.window,
                            now=datetime.now(timezone.utc),check_now=False)
            if args.preflight:
                result={"status":"PLANNED_OFFLINE_ONLY",**check}
            else:
                result=_get_result(Path(args.root).expanduser().resolve(),
                                   args.session,args.window)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return 0
    except (VenueProbeError,ValueError,TypeError,KeyError,OSError):
        # Never expose OAuth credentials, response bodies or raw KIS market data.
        print(json.dumps({"status":"FAIL_CLOSED_NO_MARKET_DATA_CERTIFIED",
                          "broker_orders":0,
                          "credentials_exposed":False}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

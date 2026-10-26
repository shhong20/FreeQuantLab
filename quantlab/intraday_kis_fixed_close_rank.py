"""Same-session KIS pre/post-close rank snapshot, read-only and nonhistorical.

KIS FHPST01760000 ranks present stocks only, not a complete market tape.
Its mkob_otcp_vol/mkfa_otcp_vol fields name fixed-close activity but
neither their finality nor the date/venue scope has been vendor verified.
No data is usable as repaired historic bars or strategy/P&L signals.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from .data import _kis_token
from .intraday_kis_cohort_capture import _comma_list, DEFAULT_SYMBOLS
from .intraday_kis_venues import _decode_response, _get, VenueProbeError

URL = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/ranking/after-hour-balance"
TR_ID = "FHPST01760000"
KST = ZoneInfo("Asia/Seoul")
PHASES = {"PRE": ("1", (8,30), (9,10), "mkob_otcp_vol"),
          "POST": ("2", (15,40), (16,20), "mkfa_otcp_vol")}
MAX_RESPONSE_ROWS = 500


def validate(*, session: str, phase: str, symbols: tuple[str, ...],
             captured_at: datetime):
    if phase not in PHASES:
        raise VenueProbeError("Only PRE/POST ranking phases allowed")
    if not (1 <= len(symbols) <= 3) or len(set(symbols)) != len(symbols):
        raise VenueProbeError("Only 1..3 unique research symbols permitted")
    if any(not isinstance(v,str) or not re.fullmatch(r"[0-9]{6}",v)
           for v in symbols):
        raise VenueProbeError("Research symbols must be six digits")
    try:
        day=date.fromisoformat(session)
    except (TypeError,ValueError) as exc:
        raise VenueProbeError("Expected session must be ISO date") from exc
    if not isinstance(session,str) or session!=day.isoformat():
        raise VenueProbeError("Expected session must be ISO date")
    if captured_at.tzinfo is None:
        raise VenueProbeError("Captured time must be timezone-aware")
    local=captured_at.astimezone(KST)
    if day != local.date() or day.weekday()>=5:
        raise VenueProbeError("Ranking API has no historical date and requires current weekday")
    _,start,end,_=PHASES[phase]
    minute=local.hour*60+local.minute
    if not (start[0]*60+start[1]<=minute<end[0]*60+end[1]):
        raise VenueProbeError("Outside bounded same-session fixed-close ranking observation window")
    return local


def _number(raw: object) -> int | None:
    return int(raw) if isinstance(raw,str) and re.fullmatch(r"[0-9]+",raw) else None


def parse(*, raw: bytes, symbols: tuple[str, ...], phase: str) -> dict:
    body=_decode_response(raw,market="J",is_daily=True)
    rows=body.get("output")
    if not isinstance(rows,list) or len(rows)>MAX_RESPONSE_ROWS:
        raise VenueProbeError("KIS after-hour rank must be bounded output array")
    selected: dict[str,dict]={}
    for row in rows:
        if not isinstance(row,dict):
            raise VenueProbeError("Invalid KIS ranking row")
        ticker=row.get("stck_shrn_iscd")
        if not isinstance(ticker,str) or not re.fullmatch(r"[0-9A-Z]{6}",ticker):
            raise VenueProbeError("Invalid KIS ranking stock code")
        if ticker not in symbols:
            continue
        if ticker in selected:
            raise VenueProbeError("Duplicate selected KIS ranking ticker")
        selected[ticker]=row
    field=PHASES[phase][3]
    results=[]
    for symbol in symbols:
        row=selected.get(symbol)
        quantity=_number(row.get(field)) if row else None
        results.append({
            "symbol":symbol,
            "present_in_returned_ranking":row is not None,
            "observed_phase_volume_candidate":quantity,
            "vendor_field":field if row is not None else None,
            "missing_not_a_zero_volume_trade":row is None,
            "invalid_or_absent_number_not_a_zero_volume_trade":
                row is not None and quantity is None,
            "snapshot_not_end_of_session_total":True,
        })
    return {
        "status":"KIS_SAME_DAY_RANK_SNAPSHOT_ONLY_UNCERTIFIED",
        "phase":phase, "tr_id":TR_ID,
        "requested_symbols":list(symbols),
        "returned_ranking_rows":len(rows),
        "matched_target_symbols":sum(x["present_in_returned_ranking"] for x in results),
        "target_observations":results,
        "historical_date_request_supported":False,
        "rank_result_could_omit_target":True,
        "ranking_pagination_exhaustively_retrieved":False,
        "phase_volume_finality_vendor_verified":False,
        "provider_market_regime_scope_verified":False,
        "source_is_not_historical_execution_tape":True,
        "can_reconcile_krx_daily_gap":False,
        "usable_for_backtesting":False,
        "orders_enabled":False,
    }


def capture(*, session: str, phase: str, symbols: tuple[str,...],
            root: str | Path, key: str, secret: str, token: str,
            now: datetime | None=None,
            getter: Callable=requests.get) -> dict:
    when = now or datetime.now(KST)
    local=validate(session=session,phase=phase,symbols=symbols,captured_at=when)
    if not all(isinstance(x,str) and x for x in (key,secret,token)):
        raise VenueProbeError("Local KIS authentication is required")
    outdir=Path(root).expanduser().resolve() / f"KIS_AFTER_HOUR_RANK_J_{session}_{phase}"
    if outdir.exists():
        raise VenueProbeError("Immutable same-day phase snapshot already exists")
    rank=PHASES[phase][0]
    params={
        "FID_INPUT_PRICE_1":"",
        "FID_COND_MRKT_DIV_CODE":"J",
        "FID_COND_SCR_DIV_CODE":"20176",
        "FID_RANK_SORT_CLS_CODE":rank,
        "FID_DIV_CLS_CODE":"0",
        "FID_INPUT_ISCD":"0000",
        "FID_TRGT_EXLS_CLS_CODE":"0",
        "FID_TRGT_CLS_CODE":"0",
        "FID_VOL_CNT":"",
        "FID_INPUT_PRICE_2":"",
    }
    response=_get(URL,params=params,key=key,secret=secret,
                  token=token,tr_id=TR_ID,getter=getter)
    report=parse(raw=response,symbols=symbols,phase=phase)
    report["session"]=session
    report["captured_at_kst"]=local.isoformat()
    report["raw_sha256"]=sha256(response).hexdigest()
    outdir.parent.mkdir(parents=True,exist_ok=True)
    staged=Path(tempfile.mkdtemp(prefix=".kis-fixed-close-rank-",dir=outdir.parent))
    try:
        (staged/"raw.json").write_bytes(response)
        audit_blob=(json.dumps(report,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        (staged/"audit.json").write_bytes(audit_blob)
        (staged/"audit.sha256").write_text(sha256(audit_blob).hexdigest()+"\n")
        if outdir.exists():
            raise VenueProbeError("Concurrent immutable ranking snapshot")
        staged.rename(outdir)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return {"output":str(outdir),"audit":report}


def inspect(*, root: str | Path, session: str, phase: str,
            symbols: tuple[str,...]) -> dict:
    folder=Path(root).expanduser().resolve() / f"KIS_AFTER_HOUR_RANK_J_{session}_{phase}"
    try:
        raw=(folder/"raw.json").read_bytes()
        report=json.loads((folder/"audit.json").read_bytes())
        saved_digest=(folder/"audit.sha256").read_text().strip()
        if sha256(raw).hexdigest()!=report.get("raw_sha256"):
            raise VenueProbeError("Vendor ranking raw source SHA mismatch")
        if sha256((folder/"audit.json").read_bytes()).hexdigest()!=saved_digest:
            raise VenueProbeError("Ranking audit SHA mismatch")
        if report.get("session")!=session or report.get("phase")!=phase:
            raise VenueProbeError("Wrong fixed-close ranking session/phase")
        if tuple(report.get("requested_symbols",()))!=symbols:
            raise VenueProbeError("Wrong fixed-close ranking symbols")
        parsed=parse(raw=raw,symbols=symbols,phase=phase)
        if any(report.get(k)!=v for k,v in parsed.items()):
            raise VenueProbeError("Saved ranking summary disagrees with raw")
        return report
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc,VenueProbeError):
            raise
        raise VenueProbeError("Cannot verify immutable ranking source") from exc


def main(argv: list[str] | None=None) -> int:
    p=argparse.ArgumentParser(description="KIS current-day fixed-close ranking probe; DRY RUN by default")
    p.add_argument("--phase",choices=tuple(PHASES),required=True)
    p.add_argument("--expected-session",required=True)
    p.add_argument("--symbols",default=DEFAULT_SYMBOLS)
    p.add_argument("--out-dir",required=True)
    p.add_argument("--execute",action="store_true")
    args=p.parse_args(argv)
    try:
        symbols=_comma_list(args.symbols)
        # The default CLI does not use OAuth, HTTP or save any data.
        local=datetime.now(KST)
        validate(session=args.expected_session,phase=args.phase,
                 symbols=symbols,captured_at=local)
        if not args.execute:
            print(json.dumps({
                "status":"DRY_RUN_NO_NETWORK",
                "phase":args.phase,
                "expected_session":args.expected_session,
                "target_symbols":list(symbols),
                "url_family":"KIS_FHPST01760000_RANKING_ONLY",
                "backtest_allowed":False,
            },sort_keys=True))
            return 0
        existing=Path(args.out_dir).expanduser().resolve()/(
            f"KIS_AFTER_HOUR_RANK_J_{args.expected_session}_{args.phase}")
        if existing.exists():
            report=inspect(root=args.out_dir,session=args.expected_session,
                           phase=args.phase,symbols=symbols)
            print(json.dumps({
                "status":"EXISTING_VERIFIED",
                "output":str(existing),
                "target_observations":report["target_observations"],
            },sort_keys=True))
            return 0
        key=os.getenv("KIS_APP_KEY","")
        secret=os.getenv("KIS_APP_SECRET","")
        if not key or not secret:
            raise VenueProbeError("KIS credentials must be supplied locally")
        token=_kis_token()
        result=capture(session=args.expected_session,phase=args.phase,
                       symbols=symbols,root=args.out_dir,
                       key=key,secret=secret,token=token,now=local)
        print(json.dumps({
            "status":result["audit"]["status"],
            "output":result["output"],
            "target_observations":result["audit"]["target_observations"],
        },sort_keys=True))
        return 0
    except Exception as exc:
        # Do not emit third-party response texts, raw requests or tokens.
        kind=type(exc).__name__
        p.exit(2,"FAIL CLOSED: "+(
            kind if kind in ("VenueProbeError","DataError") else "UnexpectedError"
        )+"\n")


if __name__=="__main__":
    raise SystemExit(main())

"""Bounded, read-only KIS historical missing-label comparison.

Queries only seven SHA-pinned suspect KRX stock-days. Compares the provider's
documented FID_FAKE_TICK_INCU_YN blank and Y modes. A returned Y-only
record MUST NEVER be interpreted as an actual exchange trade or used to
fill a research bar: 'Y' requests artificial (fake tick) inclusion.

Does not patch original source, create a backtest, place orders, or
contact any non-quotation endpoint besides OAuth.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Callable

import requests

from .data import _kis_token
from .intraday_krx_halt_position_stress import _read_verified
from .intraday_kis_venue_compare import _read_one
from .intraday_kis_probe import URL, TR_ID, FIELDS, _request_day
from .intraday_kis_venues import _get, _decode_response, VenueProbeError

MAX_STOCK_DAYS = 7
MAX_CLOCK_LABELS = 13
MAX_QUERIES = 18
PAGES_PER_MODE = 1
MODES = ("", "Y")


def _cursors(clocks: list[str]) -> list[str]:
    """One 120-row snapshot near each distinct missing-label region."""
    output=[]
    for clock in sorted(clocks):
        if not re.fullmatch(r"[0-2][0-9][0-5][0-9]00",clock):
            raise VenueProbeError("Expected exact 1-minute missing clock label")
        minutes=int(clock[:2])*60+int(clock[2:4])
        # Two missing clocks within 80min need only one nearby page.
        if output and minutes-(int(output[-1]["anchor"][:2])*60+
                                int(output[-1]["anchor"][2:4]))<=75:
            continue
        stop=min(minutes+40,15*60+30)
        output.append({"anchor":clock,
                       "cursor":f"{stop//60:02d}{stop%60:02d}00"})
    return [r["cursor"] for r in output]


def _sample(raw: bytes, *, market="J", symbol: str, session: str) -> dict:
    body=_decode_response(raw,market=market)
    values=body.get("output2")
    if not isinstance(values,list) or len(values)>120:
        raise VenueProbeError("Unexpected provider per-page minute record count")
    wanted=session.replace("-","")
    observed={}
    for row in values:
        if not isinstance(row,dict) or any(key not in row for key in FIELDS):
            raise VenueProbeError("Missing provider minute fields")
        if row["stck_bsop_date"]!=wanted:
            # A page may include earlier days, never count them as this day.
            continue
        key=str(row["stck_cntg_hour"])
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]",key):
            raise VenueProbeError("Invalid provider clock")
        canonical={k:row[k] for k in FIELDS}
        if key in observed and observed[key]!=canonical:
            raise VenueProbeError("Provider emitted conflicting duplicate clock")
        observed[key]=canonical
    return {"clock_labels":sorted(observed),
            "minute_source_sha256":sha256(raw).hexdigest(),
            "record_count_requested_date":len(observed),
            "queried_market":"J","queried_symbol":symbol,"queried_session":session,
            "unverified_vendor_timestamp_semantics":True,
            "raw_bytes":raw}


def compare(*, triage_dir: str|Path, original_root: str|Path,
            output_dir: str|Path, key: str, secret: str, token: str,
            getter: Callable=requests.get,
            pause: Callable[[float],None]=time.sleep,
            now: datetime|None=None) -> dict:
    dest=Path(output_dir).expanduser().resolve()
    if dest.exists():
        raise VenueProbeError("Immutable missing-label probe already exists")
    src=Path(original_root).expanduser().resolve()
    triage=Path(triage_dir).expanduser().resolve()
    original=_read_verified(triage,"source_triage.json")
    if (not isinstance(original,dict) or
        original.get("status")!="KRX_RAW_SOURCE_TRIAGE_HOLD_NO_PNL" or
        original.get("usable_for_backtesting") is not False or
        original.get("usable_for_paper_or_live") is not False):
        raise VenueProbeError("Untrusted historical gap triage")
    targets=original.get("unexplained_cases")
    if (not isinstance(targets,list) or len(targets)!=MAX_STOCK_DAYS or
        original.get("unexplained_missing_stock_days")!=len(targets) or
        sum(len(t.get("unknown_regular_minute_labels",[])) for t in targets)
            !=MAX_CLOCK_LABELS or
        original.get("unexplained_missing_minute_labels")!=MAX_CLOCK_LABELS or
        not all(isinstance(t,dict) for t in targets)):
        raise VenueProbeError("Historical gap population changed")
    if not all(isinstance(v,str) and v.strip() for v in (key,secret,token)):
        raise VenueProbeError("Read-only KIS bearer credentials required")
    now=now or datetime.now(timezone.utc)
    # Preflight entire population before the first network call.
    queries=[]
    for target in targets:
        symbol,session=target["symbol"],target["session"]
        if not re.fullmatch(r"[0-9]{6}",symbol):
            raise VenueProbeError("Invalid source symbol")
        _request_day(session,now)
        report,records=_read_one(src,symbol,session,"J")
        if report["source_minute_bar_sha256"]!=target["source_minute_sha256"]:
            raise VenueProbeError("Original KIS source SHA changed")
        clocks=target["unknown_regular_minute_labels"]
        if any(c in records for c in clocks):
            raise VenueProbeError("Supposedly missing minute already existed")
        cursors=_cursors(clocks)
        for cursor in cursors:
            for mode in MODES:
                queries.append((symbol,session,cursor,mode))
    if not queries or len(queries)>MAX_QUERIES:
        raise VenueProbeError("Unexpected historical minute probe count")
    staging=None
    try:
        dest.parent.mkdir(parents=True,exist_ok=True)
        staging=Path(tempfile.mkdtemp(prefix=".kis-historical-gap-",dir=dest.parent))
        records_by_case={}
        sha_files={}
        for i,(symbol,session,cursor,mode) in enumerate(queries):
            if i:pause(0.25)
            raw=_get(URL,params={
                    "FID_COND_MRKT_DIV_CODE":"J",
                    "FID_INPUT_ISCD":symbol,
                    "FID_INPUT_HOUR_1":cursor,
                    "FID_INPUT_DATE_1":session.replace("-",""),
                    "FID_PW_DATA_INCU_YN":"N",
                    "FID_FAKE_TICK_INCU_YN":mode,
                },key=key,secret=secret,token=token,tr_id=TR_ID,getter=getter)
            sample=_sample(raw,symbol=symbol,session=session)
            mode_label="blank" if mode=="" else "Y"
            filename=f"J_{symbol}_{session}_{cursor}_{mode_label}.json"
            (staging/filename).write_bytes(raw)
            sha_files[filename]=sample["minute_source_sha256"]
            k=(symbol,session)
            records_by_case.setdefault(k,{}).setdefault(mode_label,set()).update(
                sample["clock_labels"])
        cases=[]
        for target in targets:
            symbol,session=target["symbol"],target["session"]
            modes=records_by_case[(symbol,session)]
            missing=target["unknown_regular_minute_labels"]
            cases.append({
                "symbol":symbol,"session":session,
                "original_source_minute_sha256":target["source_minute_sha256"],
                "original_missing_clocks":missing,
                "probe_blank_present":sorted(set(missing)&modes.get("blank",set())),
                "probe_Y_present":sorted(set(missing)&modes.get("Y",set())),
                "probe_Y_only_present":sorted(
                    set(missing)&(modes.get("Y",set())-modes.get("blank",set()))),
                "queried_blank_label_count":len(modes.get("blank",set())),
                "queried_Y_label_count":len(modes.get("Y",set())),
                "one_page_per_cursor_not_full_day_capture":True,
                "Y_only_cannot_certify_exchange_execution":True,
                "minute_coverage_not_guaranteed_by_one_page":True,
            })
        result={
            "status":"KIS_FAKE_TICK_FLAG_COMPARISON_QUARANTINED_NO_REPAIR",
            "triage_sha256":sha256((triage/"source_triage.json").read_bytes()).hexdigest(),
            "source_population":len(cases),
            "missing_clocks_in_original":MAX_CLOCK_LABELS,
            "HTTP_GET_requests":len(queries),
            "fake_tick_Y_only_total":sum(len(t["probe_Y_only_present"]) for t in cases),
            "cases":cases,
            "raw_page_checksums":sha_files,
            "raw_source_mutated":False,
            "Y_mode_not_evidence_of_real_trades":True,
            "minute_start_end_convention_verified":False,
            "no_autofill_or_zero_fill":True,
            "usable_for_backtesting":False,
            "usable_for_paper_or_live":False,
        }
        blob=(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (staging/"probe_report.json").write_bytes(blob)
        (staging/"probe_report.sha256").write_text(sha256(blob).hexdigest()+"\n")
        if dest.exists():
            raise VenueProbeError("Concurrent immutable KIS probe output")
        staging.rename(dest)
        return result
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description="Compare KIS fake-tick flag on bounded historical holes; no orders")
    parser.add_argument("--triage-dir",required=True)
    parser.add_argument("--original-root",required=True)
    parser.add_argument("--output-dir",required=True)
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args(argv)
    if not args.execute:
        parser.exit(2,"FAIL CLOSED: explicit --execute required for historical GET queries\n")
    try:
        r=compare(triage_dir=args.triage_dir,original_root=args.original_root,
                  output_dir=args.output_dir,key=os.getenv("KIS_APP_KEY",""),
                  secret=os.getenv("KIS_APP_SECRET",""),token=_kis_token())
        print(json.dumps({k:v for k,v in r.items()
                          if k in ("status","source_population","missing_clocks_in_original",
                                   "HTTP_GET_requests","fake_tick_Y_only_total","cases")},
                         ensure_ascii=False,indent=2))
        return 0
    except (VenueProbeError,ValueError,TypeError,KeyError):
        parser.exit(2,"FAIL CLOSED: insufficient KIS historical gap evidence\n")


if __name__=="__main__":
    raise SystemExit(main())

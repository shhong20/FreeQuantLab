"""Offline OHLCV equality audit of saved KIS fake-tick blank/Y responses.

Fully SHA-pinned, no API traffic, no fills, no imputation or strategy input.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile

from .intraday_krx_halt_position_stress import _read_verified
from .intraday_kis_venues import VenueProbeError, _decode_response
from .intraday_kis_probe import FIELDS

NAME = re.compile(r"J_(\d{6})_(\d{4}-\d{2}-\d{2})_(\d{6})_(blank|Y)\.json")


def audit(*, probe_dir: str | Path, output_dir: str | Path) -> dict:
    saved=Path(probe_dir).expanduser().resolve()
    output=Path(output_dir).expanduser().resolve()
    if output.exists():
        raise VenueProbeError("Immutable pair quality audit already exists")
    manifest=_read_verified(saved,"probe_report.json")
    checks=manifest.get("raw_page_checksums") if isinstance(manifest,dict) else None
    if (not isinstance(manifest,dict) or
        manifest.get("status")!="KIS_FAKE_TICK_FLAG_COMPARISON_QUARANTINED_NO_REPAIR" or
        manifest.get("source_population")!=7 or
        manifest.get("missing_clocks_in_original")!=13 or
        manifest.get("HTTP_GET_requests")!=16 or
        not isinstance(checks,dict) or len(checks)!=16):
        raise VenueProbeError("Incomplete SHA-pinned source flag comparison")
    paired={}
    for name,digest in checks.items():
        match=NAME.fullmatch(name)
        if not match or not re.fullmatch(r"[0-9a-f]{64}",str(digest)):
            raise VenueProbeError("Invalid raw page identity")
        symbol,session,cursor,mode=match.groups()
        raw=(saved/name).read_bytes()
        if sha256(raw).hexdigest()!=digest:
            raise VenueProbeError("KIS raw page checksum mismatch")
        data=_decode_response(raw,market="J")
        rows=data.get("output2")
        if not isinstance(rows,list) or len(rows)>120:
            raise VenueProbeError("KIS raw minute page is malformed")
        observed={}
        for row in rows:
            if not isinstance(row,dict) or any(field not in row for field in FIELDS):
                raise VenueProbeError("KIS raw minute row is malformed")
            if row["stck_bsop_date"]!=session.replace("-",""):
                continue
            clock=row["stck_cntg_hour"]
            if not isinstance(clock,str) or not re.fullmatch(r"[0-2][0-9][0-5][0-9][0-5][0-9]",clock):
                raise VenueProbeError("KIS minute clock is malformed")
            canonical={field:row[field] for field in FIELDS}
            if clock in observed and observed[clock]!=canonical:
                raise VenueProbeError("Conflicting raw 1m label")
            observed[clock]=canonical
        group=paired.setdefault((symbol,session,cursor),{})
        if mode in group:
            raise VenueProbeError("Duplicate original flag mode")
        group[mode]=observed
    if len(paired)!=8 or any(set(pair)!=set(("blank","Y")) for pair in paired.values()):
        raise VenueProbeError("Expected 8 complete matched KIS query pairs")
    comparisons=[]
    for (symbol,session,cursor),modes in sorted(paired.items()):
        base,with_fake=modes["blank"],modes["Y"]
        common=set(base)&set(with_fake)
        altered=sorted(t for t in common if base[t]!=with_fake[t])
        comparisons.append({
            "symbol":symbol,"session":session,"cursor":cursor,
            "blank_count":len(base),"Y_count":len(with_fake),
            "blank_only":sorted(set(base)-set(with_fake)),
            "Y_only":sorted(set(with_fake)-set(base)),
            "changed_common_OHLCV":altered,
            "all_common_values_identical":not bool(altered),
        })
    cases=manifest.get("cases")
    if not isinstance(cases,list) or len(cases)!=7:
        raise VenueProbeError("Original missing-minute cases absent")
    for case in cases:
        symbol,session=case["symbol"],case["session"]
        base=set()
        fake=set()
        for (sy,day,_),modes in paired.items():
            if (sy,day)==(symbol,session):
                base.update(modes["blank"])
                fake.update(modes["Y"])
        missing=set(case["original_missing_clocks"])
        if (sorted(base&missing)!=case["probe_blank_present"] or
            sorted(fake&missing)!=case["probe_Y_present"] or
            sorted((fake-base)&missing)!=case["probe_Y_only_present"]):
            raise VenueProbeError("Raw response labels differ from original report")
    results={
        "status":"KIS_SAVED_FLAG_RESPONSE_VALUE_COMPARISON_NO_PNL",
        "original_probe_report_sha256":sha256(
            (saved/"probe_report.json").read_bytes()).hexdigest(),
        "raw_response_count":len(checks),
        "query_mode_pairs":len(comparisons),
        "identical_clock_label_pairs":sum(not x["blank_only"] and not x["Y_only"]
            for x in comparisons),
        "identical_all_minute_field_pairs":sum(not x["blank_only"] and
            not x["Y_only"] and x["all_common_values_identical"] for x in comparisons),
        "missing_original_clocks_restored":sum(
            len(c["probe_blank_present"])+len(c["probe_Y_present"]) for c in cases),
        "comparisons":comparisons,
        "vendor_clock_contract_verified":False,
        "missing_candles_repaired":False,
        "real_trade_or_fill_proven":False,
        "usable_for_backtesting":False,
        "usable_for_paper_or_live":False,
    }
    output.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".kis-flag-pair-",dir=output.parent))
    try:
        payload=(json.dumps(results,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        (stage/"pair_audit.json").write_bytes(payload)
        (stage/"pair_audit.sha256").write_text(sha256(payload).hexdigest()+"\n")
        if output.exists():
            raise VenueProbeError("Concurrent immutable pair audit")
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return results


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description="SHA-verified offline paired KIS source comparison")
    parser.add_argument("--probe-dir",required=True)
    parser.add_argument("--output-dir",required=True)
    args=parser.parse_args(argv)
    try:
        result=audit(probe_dir=args.probe_dir,output_dir=args.output_dir)
        print(json.dumps({k:v for k,v in result.items() if k in (
            "status","raw_response_count","query_mode_pairs",
            "identical_clock_label_pairs","identical_all_minute_field_pairs",
            "missing_original_clocks_restored","usable_for_backtesting"
        )},ensure_ascii=False,indent=2))
        return 0
    except (VenueProbeError,ValueError,TypeError,KeyError,OSError):
        parser.exit(2,"FAIL CLOSED: unverifiable KIS saved raw response comparison\n")


if __name__=="__main__":
    raise SystemExit(main())

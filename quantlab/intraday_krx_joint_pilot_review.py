"""One-day KRX-J three-source pilot closeout, independent read-only audit.

Evaluates *actual* captured source bytes and existing report after market close.
Never regenerates missing quotes, never infers orders/fills, and never calls KIS.
Any missing or invalid window remains missing/failed, not a research success.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import statistics
import tempfile
from zoneinfo import ZoneInfo

from .intraday_krx_joint_pilot import PILOT_SESSION, WINDOWS, DEFAULT_ROOT
from .intraday_krx_joint_evidence import compare
from .intraday_krx_halt_position_stress import _read_verified
from .intraday_kis_venues import VenueProbeError

KST=ZoneInfo("Asia/Seoul")
EXPECTED_TRIPLETS=4


def _verify_window(root:Path, window:str, *, recover_audit:bool=False)->tuple[dict,dict|None]:
    source=root/f"{PILOT_SESSION}_{window}_raw"
    report=root/f"{PILOT_SESSION}_{window}_audit"
    basic={"window":window,"status":"NOT_CAPTURED",
           "verified_triplets":0,"matched_ticks":0,
           "price_within_candle_range":0,"broker_orders":0}
    if not source.exists():
        if report.exists():
            basic["status"]="ORPHAN_AUDIT_WITHOUT_RAW"
        return basic,None
    if not (source/"manifest.json").exists():
        basic["status"]="INCOMPLETE_RAW_NOT_RECONSTRUCTIBLE"
        return basic,None
    if not report.exists() and not recover_audit:
        basic["status"]="RAW_CAPTURED_AUDIT_MISSING"
        return basic,None
    try:
        if not report.exists():
            # Offline audit of already captured KIS market bytes only.
            # No HTTP requests, no recapture, no synthetic ticks.
            compare(source_dir=source,out_dir=report)
        recorded=_read_verified(report,"joint_comparison.json")
        manifest=_read_verified(source,"manifest.json")
        if (recorded.get("status")!="KRX_NEAR_TIME_THREE_SOURCE_CONSISTENCY_NOT_FILL_VALIDATION" or
            manifest.get("status")!="READ_ONLY_KRX_J_NEAR_TIME_L2_TICKS_1M_NO_FILLS" or
            manifest.get("real_orders")!=0 or
            recorded.get("broker_orders")!=0 or
            manifest.get("rounds")!=2 or
            recorded.get("sample_triplets")!=EXPECTED_TRIPLETS or
            recorded.get("usable_for_backtesting") is not False or
            recorded.get("usable_for_paper_or_live") is not False or
            recorded.get("manifest_sha256")!=sha256((source/"manifest.json").read_bytes()).hexdigest()):
            raise VenueProbeError("Unsafe or incomplete KRX final report")
        # Independently recompute all source SHA checks and normalized market
        # fields. A self-consistent edited report must not pass this check.
        with tempfile.TemporaryDirectory(prefix=".pilot-reverify-",dir=root) as tmp:
            new=compare(source_dir=source,out_dir=Path(tmp)/"regenerated")
            if new!=recorded:
                raise VenueProbeError("Published report differs from independent audit")
        samples=recorded.get("per_sample")
        if (not isinstance(samples,list) or len(samples)!=EXPECTED_TRIPLETS or
            {(s.get("sample_index"),s.get("symbol")) for s in samples}!=
            {(i,sym) for i in (0,1) for sym in ("005930","000660")}):
            raise VenueProbeError("Pilot population mismatch")
        start,stop=WINDOWS[window]
        for sample in samples:
            first=datetime.fromisoformat(sample["start_kst"]).astimezone(KST)
            last=datetime.fromisoformat(sample["last_received_kst"]).astimezone(KST)
            minute=first.hour*60+first.minute
            if (first.date().isoformat()!=PILOT_SESSION or
                last.date().isoformat()!=PILOT_SESSION or
                not start<=minute<stop or
                not start<=last.hour*60+last.minute<stop or
                not 0<=(last-first).total_seconds()<=30):
                raise VenueProbeError("Pilot capture not in its true KRX time window")
        basic.update({
            "status":"VERIFIED_RESEARCH_ONLY",
            "verified_triplets":len(samples),
            "matched_ticks":recorded["matched_tick_minute_row_count"],
            "price_within_candle_range":
                recorded["matched_tick_price_within_candle_range_count"],
            "source_manifest_sha256":recorded["manifest_sha256"],
            "audit_sha256":sha256((report/"joint_comparison.json").read_bytes()).hexdigest(),
        })
        return basic,recorded
    except (VenueProbeError,ValueError,TypeError,KeyError,FileNotFoundError,OSError,PermissionError):
        basic["status"]="EVIDENCE_FAILED_VERIFICATION"
        return basic,None


def summarize(*,root:Path|str=DEFAULT_ROOT,output_dir:Path|str|None=None,
              session:str=PILOT_SESSION,
              now:datetime|None=None,
              write:bool=True)->dict:
    if session!=PILOT_SESSION:
        raise VenueProbeError("Only 2026-10-29 can be summarized")
    clock=now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise VenueProbeError("Aware time required")
    kst=clock.astimezone(KST)
    if write and (kst.strftime("%Y-%m-%d")!=PILOT_SESSION or
                  (kst.hour,kst.minute)<(15,35)):
        raise VenueProbeError("Only final 2026-10-29 market closeout permitted")
    path=Path(root).expanduser().resolve()
    if not path.is_dir():
        raise VenueProbeError("KRX pilot root is not present")
    rows=[]
    observations=[]
    for window in WINDOWS:
        outcome,verified=_verify_window(path,window,recover_audit=write)
        rows.append(outcome)
        if verified is not None:
            observations.extend(verified["per_sample"])
    good=sum(row["status"]=="VERIFIED_RESEARCH_ONLY" for row in rows)
    by_symbol={}
    for symbol in ("005930","000660"):
        samples=[s for s in observations if s["symbol"]==symbol]
        by_symbol[symbol]={
            "verified_triplets":len(samples),
            "spread_bps_median":statistics.median(
                s["book_spread_bps"] for s in samples) if samples else None,
            "spread_bps_min":min(
                (s["book_spread_bps"] for s in samples),default=None),
            "spread_bps_max":max(
                (s["book_spread_bps"] for s in samples),default=None),
        }
    result={
        "status":("ALL_THREE_WINDOWS_VERIFIED_RESEARCH_ONLY" if good==3
                  else "PARTIAL_OR_FAILED_RESEARCH_PILOT"),
        "session":session,
        "reviewed_at_kst":kst.isoformat(),
        "verified_windows":good,
        "missing_or_invalid_windows":3-good,
        "expected_windows":3,
        "expected_market_data_responses":36,
        "verified_triplets":sum(row["verified_triplets"] for row in rows),
        "market_ticks_matching_HHMM_minute_labels":sum(row["matched_ticks"] for row in rows),
        "market_ticks_within_matched_minute_range":sum(
            row["price_within_candle_range"] for row in rows),
        "window_results":rows,"by_symbol":by_symbol,
        "data_collection_failure_cause_not_inferred":True,
        "missed_live_snapshots_cannot_be_backfilled":True,
        "quote_book_executions_are_sequential_not_atomic":True,
        "market_trades_are_not_personal_order_fills":True,
        "historical_strategy_returns":None,
        "broker_orders":0,
        "backtesting_approval":False,
        "paper_or_live_approval":False,
    }
    if write:
        target=Path(output_dir or (path/"2026-10-29_final_review")).expanduser().resolve()
        if target.exists():
            raise VenueProbeError("Immutable pilot closeout already written")
        target.parent.mkdir(parents=True,exist_ok=True)
        stage=Path(tempfile.mkdtemp(prefix=".krx-pilot-close-",dir=target.parent))
        try:
            payload=(json.dumps(result,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
            (stage/"final_review.json").write_bytes(payload)
            (stage/"final_review.sha256").write_text(sha256(payload).hexdigest()+"\n")
            if target.exists():
                raise VenueProbeError("Concurrent final KRX closeout")
            stage.rename(target)
        finally:
            if stage.exists():shutil.rmtree(stage)
    return result


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description="One-day KRX 3-source pilot final review (offline)")
    parser.add_argument("--session",default=PILOT_SESSION)
    parser.add_argument("--root",default=str(DEFAULT_ROOT))
    parser.add_argument("--output-dir")
    parser.add_argument("--preview",action="store_true")
    args=parser.parse_args(argv)
    try:
        result=summarize(root=args.root,session=args.session,
                         output_dir=args.output_dir,write=not args.preview)
        print(json.dumps({
            "status":result["status"],
            "verified_windows":result["verified_windows"],
            "verified_triplets":result["verified_triplets"],
            "market_ticks_matching_HHMM_minute_labels":
                result["market_ticks_matching_HHMM_minute_labels"],
            "windows":{r["window"]:r["status"] for r in result["window_results"]},
            "broker_orders":0,
        },sort_keys=True,ensure_ascii=False))
        return 0 if result["verified_windows"]==3 or args.preview else 1
    except (VenueProbeError,ValueError,TypeError,KeyError,OSError):
        print(json.dumps({"status":"FAIL_CLOSED_NO_VERIFIED_REPORT",
                          "broker_orders":0}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

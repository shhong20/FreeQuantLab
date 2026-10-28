"""SHA-pinned independent source-quality triage for KIS KRX 100-session data.

Explains observed minute gaps and closing labels; never repairs/mints candles.
A 15:32 closing print matching official daily close is a price consistency
check, NOT proof of a 15:30 auction execution timestamp.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile

from .intraday_krx_5m_preview import CLOCKS
from .intraday_krx_backfill import load as load_plan
from .intraday_krx_halt_position_stress import _read_verified, _validated
from .intraday_kis_venue_compare import _read_one
from .intraday_kis_venues import VenueProbeError


def inspect_one(*, symbol: str, session: str, report: dict,
                observed: dict[str,dict], day: dict) -> dict:
    if (report.get("symbol")!=symbol or report.get("session")!=session or
        report.get("market")!="J" or
        report.get("source_minute_bar_sha256")!=day.get("source_minute_sha256")):
        raise VenueProbeError("Source provenance does not match risk-gated day")
    daily=report.get("daily_comparison")
    entries=(daily or {}).get("date_only_public_fields")
    if (not isinstance(daily,dict) or daily.get("requested_date_matches")!=1 or
        not isinstance(entries,list) or len(entries)!=1 or
        not isinstance(entries[0],dict) or
        entries[0].get("stck_bsop_date")!=session.replace("-","") or
        not re.fullmatch(r"[0-9]+",str(entries[0].get("stck_clpr","")))):
        raise VenueProbeError("Official daily close identity unverified")
    daily_close=int(entries[0]["stck_clpr"])
    last=[x for x in sorted(observed) if "152000" <= x <= "154000"]
    label=last[-1] if last else None
    close=int(observed[label]["stck_prpr"]) if label else None
    # The expected set is 380 regular continuous 1m labels (09:00-15:19).
    missing=sorted(set(CLOCKS)-set(observed))
    if len(missing)!=day.get("missing_regular_minute_count"):
        raise VenueProbeError("Source missing clocks differ from pinned 5m audit")
    expected_marker=day.get("observed_closing_clock_candidates")
    if expected_marker!=last:
        raise VenueProbeError("Closing label changed since risk audit")
    previous_next=[]
    regular=set(CLOCKS)
    for clock in missing:
        idx=CLOCKS.index(clock)
        before=next((CLOCKS[j] for j in range(idx-1,-1,-1)
                     if CLOCKS[j] in observed),None)
        after=next((CLOCKS[j] for j in range(idx+1,len(CLOCKS))
                    if CLOCKS[j] in observed),None)
        previous_next.append({
            "minute_clock":clock,
            "before_clock":before,
            "after_clock":after,
            "before_close":int(observed[before]["stck_prpr"]) if before else None,
            "after_open":int(observed[after]["stck_oprc"]) if after else None,
            "no_fabricated_missing_price":True,
        })
    disposition=day.get("day_disposition")
    return {
        "symbol":symbol,"session":session,
        "source_minute_sha256":report["source_minute_bar_sha256"],
        "risk_disposition":disposition,
        "observed_regular_minute_labels":len(regular & set(observed)),
        "unknown_regular_minute_labels":missing,
        "unknown_minute_neighbors_not_imputations":previous_next,
        "source_page_count":report.get("pages"),
        "provider_pagination_stop":report.get("pagination_stop"),
        "first_observed_labels":report.get("sample_first_labels"),
        "last_close_label_observed":label,
        "last_close_label_price":close,
        "unadjusted_daily_close":daily_close,
        "last_observed_close_matches_daily":close == daily_close,
        "close_marker_is_153200":label=="153200",
        "closing_auction_time_semantics_verified":False,
        "vendor_minute_start_end_contract_verified":False,
        "missing_clock_may_indicate_provider_gap_or_absent_trade":True,
        "missing_clocks_are_zero_trading":False,
        "corrections_or_order_execution_allowed":False,
    }


def audit(*, plan_dir: str|Path, halt_root: str|Path,
          output_dir: str|Path) -> dict:
    target=Path(output_dir).expanduser().resolve()
    if target.exists():
        raise VenueProbeError("Immutable KRX source triage exists")
    plan=load_plan(plan_dir)
    root=Path(plan["source_root"]).expanduser().resolve()
    risk_root=Path(halt_root).expanduser().resolve()
    original=_read_verified(risk_root,"halt_5m_summary.json")
    if (not isinstance(original,dict) or
        original.get("status")!="HALT_AWARE_5M_CANDIDATES_RESEARCH_ONLY_NO_PNL" or
        original.get("usable_for_backtesting") is not False or
        original.get("usable_for_paper_or_live") is not False or
        original.get("sessions")!=plan["sessions"] or
        original.get("symbols")!=plan["symbols"]):
        raise VenueProbeError("Historical plan and safety-gated data mismatch")
    rows=[]
    counts=Counter()
    for symbol in plan["symbols"]:
        days=_validated(risk_root,symbol,plan["sessions"])
        for day in days:
            source_report,observed=_read_one(
                root,symbol,day["session"],"J"
            )
            record=inspect_one(symbol=symbol,session=day["session"],
                               report=source_report,observed=observed,day=day)
            counts[day["day_disposition"]]+=1
            rows.append(record)
    if (len(rows)!=len(plan["symbols"])*len(plan["sessions"]) or
        dict(counts)!=original.get("day_dispositions")):
        raise VenueProbeError("Source-quality population mismatch")
    unknown=[r for r in rows if
             r["risk_disposition"].endswith("UNKNOWN_GAPS")]
    irregular=[r for r in rows if
               r["risk_disposition"].endswith("END_MARKER_UNVERIFIED")]
    marker_disagreement=[r for r in rows if
                         not r["last_observed_close_matches_daily"]]
    result={
        "status":"KRX_RAW_SOURCE_TRIAGE_HOLD_NO_PNL",
        "plan_sha256":sha256(
            (Path(plan_dir)/"plan.json").read_bytes()).hexdigest(),
        "risk_summary_sha256":sha256(
            (risk_root/"halt_5m_summary.json").read_bytes()).hexdigest(),
        "total_stock_days":len(rows),
        "unexplained_missing_stock_days":len(unknown),
        "unexplained_missing_minute_labels":sum(
            len(r["unknown_regular_minute_labels"]) for r in unknown),
        "unexplained_cases":unknown,
        "unverified_close_marker_cases":irregular,
        "unverified_close_marker_count":len(irregular),
        "last_observed_close_daily_mismatch_count":len(marker_disagreement),
        "last_observed_close_daily_mismatch_cases":marker_disagreement,
        "all_stock_day_records":rows,
        "missing_minute_is_a_proven_zero_volume_trade":False,
        "close_price_parity_does_not_certify_timestamp_semantics":True,
        "raw_sources_repaired":False,
        "usable_for_backtesting":False,
        "usable_for_paper_or_live":False,
    }
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".krx-raw-triage-",dir=target.parent))
    try:
        blob=(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (stage/"source_triage.json").write_bytes(blob)
        (stage/"source_triage.sha256").write_text(sha256(blob).hexdigest()+"\n")
        if target.exists():
            raise VenueProbeError("Concurrent KRX source triage")
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return result


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="KIS historical minute triage; no imputation")
    p.add_argument("--plan-dir",required=True)
    p.add_argument("--halt-root",required=True)
    p.add_argument("--output-dir",required=True)
    args=p.parse_args(argv)
    try:
        result=audit(plan_dir=args.plan_dir,halt_root=args.halt_root,
                     output_dir=args.output_dir)
        print(json.dumps({
            "status":result["status"],
            "total_stock_days":result["total_stock_days"],
            "unexplained_missing_stock_days":result["unexplained_missing_stock_days"],
            "unexplained_missing_minute_labels":result["unexplained_missing_minute_labels"],
            "unverified_close_marker_count":result["unverified_close_marker_count"],
            "last_observed_close_daily_mismatch_count":
                result["last_observed_close_daily_mismatch_count"],
            "usable_for_backtesting":False,
        },ensure_ascii=False,sort_keys=True,indent=2))
        return 0
    except (VenueProbeError,ValueError,TypeError,KeyError):
        p.exit(2,"FAIL CLOSED: vendor source triage cannot verify provenance\n")


if __name__=="__main__":
    raise SystemExit(main())

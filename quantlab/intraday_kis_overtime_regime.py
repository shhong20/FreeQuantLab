"""Offline, hash-verified KIS obsolete overtime single-price field audit.

September 14 2026 KRX introduced a continuous after-market and abolished
the legacy 16:00-18:00 single-price session. A zero in historical
ovtm_untp_vol is NOT evidence that all out-of-session trading was zero.
"""
from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile

from .intraday_kis_cohort_capture import DEFAULT_DATES, DEFAULT_SYMBOLS, _comma_list, plan
from .intraday_kis_overtime_daily import _read_snapshot
from .intraday_kis_venue_compare import _read_one
from .intraday_kis_venues import VenueProbeError

REFORM_DATE = "20260914"
FIELDS = ("ovtm_untp_vol", "ovtm_untp_prpr", "ovtm_untp_tr_pbmn")


def _quantity(value: object) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _date_rows(root: Path, symbol: str, sessions: tuple[str, ...]) -> tuple[dict,dict]:
    verified = _read_snapshot(root, symbol, sessions)
    try:
        payload = json.loads(
            (root / f"KIS_OVERTIME_DAILY_J_{symbol}" / "raw.json").read_bytes()
        )
    except (OSError, ValueError) as exc:
        raise VenueProbeError("Cannot parse verified overtime source") from exc
    rows = payload.get("output2")
    if not isinstance(rows, list) or len(rows) != verified["response_records"]:
        raise VenueProbeError("Vendor overtime daily raw count mismatch")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise VenueProbeError("Vendor overtime daily record invalid")
        stamp = row.get("stck_bsop_date")
        if not isinstance(stamp,str) or not re.fullmatch(r"[0-9]{8}",stamp):
            raise VenueProbeError("Vendor overtime business date invalid")
        try:
            date.fromisoformat(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}")
        except ValueError as exc:
            raise VenueProbeError("Vendor overtime business date invalid") from exc
        if stamp in result:
            raise VenueProbeError("Vendor overtime duplicate business date")
        result[stamp]=row
    return verified,result


def inspect(*, overtime_root: str | Path, cohort_root: str | Path,
            symbols: tuple[str,...], sessions: tuple[str,...],
            out_dir: str | Path) -> dict:
    # Same constraints used in other historical quality audits.
    plan(symbols=",".join(symbols), sessions=",".join(sessions), markets="J")
    overtime = Path(overtime_root).expanduser().resolve()
    cohort = Path(cohort_root).expanduser().resolve()
    output_dir = Path(out_dir).expanduser().resolve()
    report_file = output_dir / "legacy_regime_scope.json"
    if report_file.exists():
        raise VenueProbeError("Immutable legacy regime audit already exists")
    timeline = []
    comparisons = []
    for symbol in symbols:
        source, rows = _date_rows(overtime, symbol, sessions)
        pre = [r for t,r in rows.items() if t < REFORM_DATE]
        post = [r for t,r in rows.items() if t >= REFORM_DATE]
        def describe(section: list[dict]) -> dict:
            return {
                "trading_day_rows":len(section),
                "fields": {
                    field: {
                        "zero": sum(_quantity(v.get(field)) == 0 for v in section),
                        "nonzero": sum(_quantity(v.get(field)) is not None and
                                       _quantity(v.get(field)) != 0 for v in section),
                        "invalid_or_missing": sum(
                            _quantity(v.get(field)) is None for v in section
                        ),
                    } for field in FIELDS
                }
            }
        pre_stats, post_stats = describe(pre), describe(post)
        timeline.append({
            "symbol":symbol,
            "source_sha256":source["raw_sha256"],
            "first_observed":min(rows) if rows else None,
            "last_observed":max(rows) if rows else None,
            "legacy_transition_date":REFORM_DATE,
            "before_transition":pre_stats,
            "on_or_after_transition":post_stats,
            "post_transition_overtime_single_price_price_all_zero": (
                bool(post) and post_stats["fields"]["ovtm_untp_prpr"]["zero"] == len(post)
            ),
            "legacy_volume_all_zero_even_before_transition": (
                bool(pre) and pre_stats["fields"]["ovtm_untp_vol"]["zero"] == len(pre)
            ),
            "interpretation":"LEGACY_SINGLE_PRICE_RETIRED_CANDIDATE_NOT_ALL_AFTERHOURS",
            "absence_of_single_price_trading_not_proven":True,
        })
        for session in sessions:
            stamp=session.replace("-","")
            old_row=rows.get(stamp)
            # Require a corresponding KIS J snapshot, with original SHA verified.
            report, observations=_read_one(cohort,symbol,session,"J")
            del observations
            daily=report["daily_comparison"]
            values=daily.get("date_only_public_fields",[])
            if daily.get("requested_date_matches") != 1 or len(values) != 1:
                raise VenueProbeError("Official KRX daily match unavailable")
            j_daily=values[0]
            if not isinstance(j_daily,dict):
                raise VenueProbeError("Official KRX daily invalid")
            old_acml=_quantity(old_row.get("acml_vol")) if old_row else None
            krx_acml=_quantity(j_daily.get("acml_vol"))
            close_old=_quantity(old_row.get("stck_clpr")) if old_row else None
            close_krx=_quantity(j_daily.get("stck_clpr"))
            legacy_vol=_quantity(old_row.get("ovtm_untp_vol")) if old_row else None
            comparisons.append({
                "symbol":symbol, "session":session,
                "returned_day":old_row is not None,
                "historical_legacy_single_price_volume_field":legacy_vol,
                "legacy_volume_zero_not_evidence_of_no_afterhours_trades":True,
                "overtime_acml_vol":old_acml,
                "krx_official_daily_acml_vol":krx_acml,
                "overtime_daily_accumulation_parity": (
                    old_acml==krx_acml if old_acml is not None and
                    krx_acml is not None else None
                ),
                "overtime_close":close_old,
                "krx_official_daily_close":close_krx,
                "overtime_daily_close_parity": (
                    close_old==close_krx if close_old is not None and
                    close_krx is not None else None
                ),
                "legacy_field_disposition":"LEGACY_UNIT_PRICE_NOT_KRX_AFTER_MARKET",
            })
    matched=[x for x in comparisons if x["returned_day"]]
    outcome={
        "status":"LEGACY_SINGLE_PRICE_FIELD_INAPPLICABLE_TO_AFTERMARKET_CANDIDATE",
        "market_rule_change_date":REFORM_DATE,
        "legacy_market_1600_1800_abolished":True,
        "krx_after_market_1600_2000_not_same_product":True,
        "investigated_symbols":list(symbols),
        "requested_sessions":list(sessions),
        "source_timelines":timeline,
        "session_comparisons":comparisons,
        "requested_case_count":len(comparisons),
        "matched_case_count":len(matched),
        "all_matched_daily_volume_parity": (
            bool(matched) and all(
                x["overtime_daily_accumulation_parity"] is True for x in matched)
        ),
        "all_matched_daily_close_parity": (
            bool(matched) and all(
                x["overtime_daily_close_parity"] is True for x in matched)
        ),
        "field_scope_vendor_certified":False,
        "legacy_zero_does_not_reconcile_daily_minute_gaps":True,
        "additional_out_of_session_volume_found":False,
        "no_synthetic_volume_correction":True,
        "usable_for_backtesting":False,
        "usable_for_paper_or_live":False,
    }
    output_dir.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".legacy-kis-",dir=output_dir))
    try:
        blob=(json.dumps(outcome,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (stage/"legacy_regime_scope.json").write_bytes(blob)
        (stage/"legacy_regime_scope.sha256").write_text(
            sha256(blob).hexdigest()+"\n",encoding="utf-8"
        )
        if report_file.exists():
            raise VenueProbeError("Concurrent immutable scope audit")
        (stage/"legacy_regime_scope.json").rename(report_file)
        (stage/"legacy_regime_scope.sha256").rename(
            output_dir/"legacy_regime_scope.sha256"
        )
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"output":str(report_file),"audit":outcome}


def main(argv: list[str] | None = None) -> int:
    p=argparse.ArgumentParser(description="Read-only legacy overtime vs KRX daily scope")
    p.add_argument("--overtime-root",required=True)
    p.add_argument("--cohort-root",required=True)
    p.add_argument("--out-dir",required=True)
    p.add_argument("--symbols",default=DEFAULT_SYMBOLS)
    p.add_argument("--sessions",default=DEFAULT_DATES)
    args=p.parse_args(argv)
    try:
        out=inspect(overtime_root=args.overtime_root,
                    cohort_root=args.cohort_root, out_dir=args.out_dir,
                    symbols=_comma_list(args.symbols),
                    sessions=_comma_list(args.sessions))
        print(json.dumps(out,ensure_ascii=False,sort_keys=True,indent=2))
        return 0
    except VenueProbeError as exc:
        p.exit(2, f"FAIL CLOSED: {type(exc).__name__}\n")

if __name__=="__main__":
    raise SystemExit(main())

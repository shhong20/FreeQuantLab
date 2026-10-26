"""Offline cohort analysis of independent KIS J/NX/UN source snapshots.

Counts only independently complete three-venue source sets; missing snapshots
are missing, NEVER treated as zero volume or a zero-error comparison.
No KIS token, HTTP requests, stock orders or operating database writes.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

from .intraday_kis_cohort_capture import DEFAULT_SYMBOLS, DEFAULT_DATES, _comma_list, plan
from .intraday_kis_venue_compare import _read_one, audit_markets
from .intraday_kis_venues import VenueProbeError


def _existing_audit(path: Path, *, root: Path, symbol: str, session: str) -> dict:
    try:
        report = json.loads((path / "audit.json").read_bytes())
        if (not isinstance(report, dict) or
                report.get("status") !=
                "KIS_J_NX_UN_SOURCE_COMPARISON_NOT_TRADE_RECONCILIATION" or
                report.get("session") != session or report.get("symbol") != symbol or
                report.get("usable_for_backtesting") is not False):
            raise VenueProbeError("Existing cohort case identity/status invalid")
        for market in ("J", "NX", "UN"):
            _read_one(root, symbol, session, market)
            # The source's full normalized observations are pinned to the
            # audit; no silent source substitution on resumptions.
            source = json.loads((root /
                f"KIS_{market}_EXT_{symbol}_{session}" / "audit.json").read_bytes())
            expected = report["market_sources"][market]["source_sha256"]
            if source["source_minute_bar_sha256"] != expected:
                raise VenueProbeError("Case references a changed market snapshot")
        return report
    except (OSError, KeyError, ValueError, TypeError) as exc:
        if isinstance(exc, VenueProbeError):
            raise
        raise VenueProbeError("Cannot revalidate existing case comparison") from exc


def cohort(*, source_root: str | Path, symbols: tuple[str, ...],
           sessions: tuple[str, ...], output: str | Path) -> dict:
    # Identical bounded source-plan validation, including path-safe symbols
    # and historical weekdays. Caller must not be able to escape root.
    plan(symbols=",".join(symbols), sessions=",".join(sessions),
         markets="J,NX,UN")
    base = Path(source_root).expanduser().resolve()
    dest = Path(output).expanduser().resolve()
    if (dest / "cohort_summary.json").exists():
        raise VenueProbeError("Immutable cohort summary already exists")
    case_dir = dest / "cases"
    complete_cases = []
    incomplete_cases = []
    total_checked_minutes = 0
    total_market_sum_mismatches = 0
    total_price_envelope_mismatches = 0
    total_daily_summation_mismatches = 0
    for session in sessions:
        for symbol in symbols:
            present = [
                market for market in ("J", "NX", "UN")
                if (base / f"KIS_{market}_EXT_{symbol}_{session}").is_dir()
            ]
            missing = [m for m in ("J", "NX", "UN") if m not in present]
            if missing:
                # Even partial data remains independently queryable, but
                # not a valid three-market additivity test.
                for m in present:
                    _read_one(base, symbol, session, m)
                incomplete_cases.append({
                    "symbol":symbol, "session":session,
                    "missing_markets":missing,
                    "status":"NOT_COMPARABLE_MISSING_SNAPSHOT",
                })
                continue
            case = case_dir / f"KIS_J_NX_UN_{symbol}_{session}"
            if case.exists():
                audit = _existing_audit(
                    case, root=base, symbol=symbol, session=session
                )
            else:
                audit = audit_markets(
                    root=base, symbol=symbol, session=session,
                    out_dir=case_dir
                )["audit"]
            minute = audit["minute_volume_arithmetic_diagnostic"]
            price = audit["minute_price_envelope_diagnostic"]
            daily = audit["official_daily_volume_arithmetic_diagnostic"]
            overlaps = audit["market_sources"]["UN"]["minute_labels"]
            discrepancy_count = int(minute["mismatch_count"])
            envelope_count = int(price["mismatch_count"])
            if daily["status"] == "MISMATCH_HOLD":
                total_daily_summation_mismatches += 1
            total_checked_minutes += overlaps
            total_market_sum_mismatches += discrepancy_count
            total_price_envelope_mismatches += envelope_count
            complete_cases.append({
                "symbol": symbol, "session":session,
                "status": "MISMATCH_HOLD" if discrepancy_count or envelope_count or (
                    daily["status"]!="ARITHMETIC_MATCH_ONLY"
                ) else "ARITHMETIC_MATCH_UNVERIFIED",
                "integrated_observed_minute_labels": overlaps,
                "minute_volume_mismatch_count": discrepancy_count,
                "minute_volume_mismatch_labels": [
                    x["label"] for x in minute["mismatches"]
                ],
                "minute_price_envelope_mismatch_count": envelope_count,
                "minute_price_envelope_mismatch_labels": [
                    x["label"] for x in price["mismatches"]
                ],
                "official_daily_volume_additivity": daily["status"],
                "minute_volume_delta_un_minus_venues":
                    minute["minute_integrated_minus_venue_sum"],
                "official_daily_delta_un_minus_venues":
                    daily["integrated_minus_venue_volume"],
                "per_market_minute_minus_daily_volume":
                    daily["minute_minus_official_daily_volume"],
                "source_market_sha256": {
                    v:audit["market_sources"][v]["source_sha256"]
                    for v in ("J","NX","UN")
                },
                "usable_for_backtesting": False,
            })
    result = {
        "status": (
            "INSUFFICIENT_COMPLETE_CASES" if not complete_cases
            else "COHORT_WITH_INCOMPLETE_CASES" if incomplete_cases
            else "COHORT_SOURCE_AUDIT_ONLY"
        ),
        "symbols":list(symbols), "sessions":list(sessions),
        "requested_case_count":len(symbols)*len(sessions),
        "complete_three_market_case_count":len(complete_cases),
        "incomplete_three_market_case_count":len(incomplete_cases),
        "checked_integrated_minute_labels":total_checked_minutes,
        "minute_volume_mismatch_count":total_market_sum_mismatches,
        "minute_price_envelope_mismatch_count":total_price_envelope_mismatches,
        "official_daily_volume_additivity_mismatch_case_count":
            total_daily_summation_mismatches,
        "minute_mismatch_rate_denominator":
            "Only integrated minute labels in independently complete J/NX/UN cases",
        "minute_volume_mismatch_rate": (
            total_market_sum_mismatches / total_checked_minutes
            if total_checked_minutes else None
        ),
        "case_results":complete_cases,
        "excluded_missing_cases":incomplete_cases,
        "no_missing_case_counted_as_zero": True,
        "result_does_not_certify_venue_trade_identity": True,
        "provider_minute_timestamp_semantics_approved": False,
        "usable_for_backtesting": False,
        "usable_for_paper_or_live": False,
    }
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / "cohort_summary.json"
    if final.exists():
        raise VenueProbeError("Immutable cohort summary already exists")
    blob=(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False)+"\n").encode()
    stage = Path(tempfile.mkdtemp(prefix=".cohort-summary-", dir=dest))
    try:
        (stage/"cohort_summary.json").write_bytes(blob)
        (stage/"cohort_summary.sha256").write_text(sha256(blob).hexdigest()+"\n")
        (stage/"cohort_summary.json").rename(final)
        (stage/"cohort_summary.sha256").rename(dest/"cohort_summary.sha256")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"output":str(final), "summary":result}


def main(argv: list[str] | None = None) -> int:
    p=argparse.ArgumentParser(
        description="Offline multi-day KIS venue quality assessment; no trading"
    )
    p.add_argument("--source-root",required=True)
    p.add_argument("--out-dir",required=True)
    p.add_argument("--symbols",default=DEFAULT_SYMBOLS)
    p.add_argument("--sessions",default=DEFAULT_DATES)
    args=p.parse_args(argv)
    try:
        result=cohort(
            source_root=args.source_root, output=args.out_dir,
            symbols=_comma_list(args.symbols),
            sessions=_comma_list(args.sessions),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except VenueProbeError as exc:
        # Static diagnostics only; never embed KIS response body or secrets.
        p.exit(2, "FAIL CLOSED: " + str(exc) + "\n")


if __name__=="__main__":
    raise SystemExit(main())

"""Offline market-phase coverage and official-daily volume bridge.

Designed for KIS J/NX/UN historical snapshots. Calendar windows reflect
exchange schedules, not a guarantee of executions. Missing time labels are
not a claim of zero trades. Never reconciles by fabricating 1m OHLCV.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import shutil

from .intraday_kis_cohort_capture import DEFAULT_SYMBOLS, DEFAULT_DATES, plan, _comma_list
from .intraday_kis_venue_compare import _read_one
from .intraday_kis_venues import VenueProbeError


VENUES = ("J", "NX", "UN")


def clock_labels(start_hour: int, start_minute: int,
                 end_hour: int, end_minute: int) -> tuple[str, ...]:
    begin, end = start_hour*60+start_minute, end_hour*60+end_minute
    if not (0 <= begin < end <= 24*60):
        raise VenueProbeError("Invalid time-window range")
    return tuple(f"{i//60:02d}{i%60:02d}00" for i in range(begin, end))


def _window(labels: set[str], *, code: str, start: tuple[int, int],
            end: tuple[int, int], meaning: str) -> dict:
    expected = clock_labels(*start, *end)
    present = sum(x in labels for x in expected)
    return {
        "code": code,
        "time_range_local": f"{start[0]:02d}:{start[1]:02d}-{end[0]:02d}:{end[1]:02d}",
        "meaning": meaning,
        "reference_minute_labels": len(expected),
        "present_labels": present,
        "absent_labels": len(expected)-present,
        "absence_is_not_proof_of_zero_trades": True,
    }


def _official_volume(audit: dict) -> int | None:
    day = audit.get("daily_comparison")
    if not isinstance(day, dict) or day.get("requested_date_matches") != 1:
        return None
    rows=day.get("date_only_public_fields")
    if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):
        return None
    value=rows[0].get("acml_vol")
    if not isinstance(value,str) or not value.isdecimal():
        return None
    return int(value)


def analyze_case(*, root: Path, symbol: str, session: str) -> dict:
    datasets = {}
    for market in VENUES:
        audit, observed = _read_one(root, symbol, session, market)
        datasets[market] = {
            "audit": audit, "observed": observed,
            "labels": set(observed),
            "minute_volume": sum(int(row["cntg_vol"]) for row in observed.values()),
            "daily_volume": _official_volume(audit),
        }
    j, nx, un = (datasets[m] for m in VENUES)
    gaps = {
        m: (datasets[m]["daily_volume"] - datasets[m]["minute_volume"]
            if datasets[m]["daily_volume"] is not None else None)
        for m in VENUES
    }
    clocks_un=un["labels"]
    union_clocks=j["labels"]|nx["labels"]
    minute_delta=sum(
        int(un["observed"][t]["cntg_vol"]) -
        int(j["observed"].get(t,{}).get("cntg_vol",0)) -
        int(nx["observed"].get(t,{}).get("cntg_vol",0))
        for t in clocks_un
    ) if clocks_un == union_clocks else None

    windows = {
        "J": [
            _window(j["labels"],code="KRX_PRE_OPEN_FIXED_PRICE",start=(8,30),
                    end=(8,40),meaning="KRX 08:30-08:40 premarket fixed-price"),
            _window(j["labels"],code="KRX_REGULAR_CONTINUOUS",start=(9,0),
                    end=(15,20),meaning="KRX 09:00-15:20 continuous"),
            _window(j["labels"],code="KRX_AFTER_FIXED_PRICE",start=(15,40),
                    end=(16,0),meaning="KRX 15:40-16:00 fixed-price"),
            _window(j["labels"],code="KRX_AFTER_CONTINUOUS",start=(16,0),
                    end=(20,0),meaning="KRX 16:00-20:00 after market"),
        ],
        "NX": [
            _window(nx["labels"],code="NXT_PRE",start=(8,0),
                    end=(8,50),meaning="NXT 08:00-08:50 premarket"),
            _window(nx["labels"],code="NXT_MAIN",start=(9,0),
                    end=(15,20),meaning="NXT 09:00:30-15:20 main; first minute partial"),
            _window(nx["labels"],code="NXT_AFTER",start=(15,40),
                    end=(20,0),meaning="NXT 15:40-20:00 after market"),
        ],
        "UN": [
            _window(un["labels"],code="INTEGRATED_AFTER_FIXED_PRICE_CLOCK",start=(15,40),
                    end=(16,0),meaning="Integrated clock labels; not KRX fixed-price proof"),
        ],
    }
    daily_total = {m:datasets[m]["daily_volume"] for m in VENUES}
    daily_additive = (
        daily_total["J"]+daily_total["NX"]==daily_total["UN"]
        if all(v is not None for v in daily_total.values()) else None
    )
    bridge = (
        gaps["UN"] == gaps["J"]+gaps["NX"]-minute_delta
        if minute_delta is not None and all(v is not None for v in gaps.values())
        and daily_additive is True else None
    )
    return {
        "symbol": symbol, "session": session,
        "market": {
            m: {
                "normalized_observations_sha256":
                    datasets[m]["audit"]["source_minute_bar_sha256"],
                "unique_observed_clock_labels": len(datasets[m]["labels"]),
                "first_observed": min(datasets[m]["labels"]),
                "last_observed": max(datasets[m]["labels"]),
                "observed_minute_volume": datasets[m]["minute_volume"],
                "official_daily_volume": datasets[m]["daily_volume"],
                "daily_minus_minute_volume": gaps[m],
                "daily_minus_minute_volume_pct": (
                    100*gaps[m]/datasets[m]["daily_volume"]
                    if gaps[m] is not None and datasets[m]["daily_volume"] else None
                ),
                "calendar_window_coverage": windows[m],
            }
            for m in VENUES
        },
        "daily_volume_j_plus_nx_equals_un": daily_additive,
        "un_clock_labels_equal_venue_union": clocks_un==union_clocks,
        "minute_volume_un_minus_venue_sum": minute_delta,
        "daily_gap_accounting_identity_holds": bridge,
        "accounting_identity_is_only_algebra_not_execution_attribution": True,
        "missing_krx_fixed_price_minutes_do_not_prove_trade_volume": True,
        "timestamp_bucket_start_or_end": "NOT_VENDOR_VERIFIED",
        "official_dailies_can_cover_more_phases_than_historical_minutes": "PLAUSIBLE_UNVERIFIED",
        "eligible_for_backtest": False,
    }


def audit(*, source_root: str | Path, symbols: tuple[str, ...],
          sessions: tuple[str, ...], out_dir: str | Path) -> dict:
    plan(symbols=",".join(symbols),sessions=",".join(sessions),markets="J,NX,UN")
    root=Path(source_root).expanduser().resolve()
    dest=Path(out_dir).expanduser().resolve()
    if (dest/"coverage_report.json").exists():
        raise VenueProbeError("Immutable coverage report already exists")
    matched=[]
    missing=[]
    for session in sessions:
        for symbol in symbols:
            absent=[m for m in VENUES
                    if not (root/f"KIS_{m}_EXT_{symbol}_{session}").is_dir()]
            if absent:
                missing.append({"session":session,"symbol":symbol,
                                "missing_venues":absent})
                continue
            matched.append(analyze_case(root=root,symbol=symbol,session=session))
    result={
        "status":"MARKET_PHASE_COVERAGE_HOLD",
        "cases":matched,
        "unmatched_cases":missing,
        "compared_case_count":len(matched),
        "unmatched_case_count":len(missing),
        "krx_fixed_price_clock_labels_absent_in_every_compared_case": (
            bool(matched) and all(
                next(w for w in x["market"]["J"]["calendar_window_coverage"]
                     if w["code"]=="KRX_AFTER_FIXED_PRICE")["present_labels"]==0
                for x in matched
            )
        ),
        "all_official_daily_market_volume_sums_match": (
            bool(matched) and all(
                x["daily_volume_j_plus_nx_equals_un"] is True for x in matched
            )
        ),
        "all_accounting_identities_hold": (
            bool(matched) and all(
                x["daily_gap_accounting_identity_holds"] is True for x in matched
            )
        ),
        "complete_comparisons_only": True,
        "no_missing_phase_volume_imputation": True,
        "provider_time_bucket_semantics_verified": False,
        "usable_for_backtesting": False,
        "usable_for_paper_or_live": False,
    }
    dest.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".kis-coverage-",dir=dest))
    blob=(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
    try:
        (stage/"coverage_report.json").write_bytes(blob)
        (stage/"coverage_report.sha256").write_text(
            sha256(blob).hexdigest()+"\n",encoding="utf-8")
        if (dest/"coverage_report.json").exists():
            raise VenueProbeError("Concurrent immutable coverage report")
        (stage/"coverage_report.json").rename(dest/"coverage_report.json")
        (stage/"coverage_report.sha256").rename(dest/"coverage_report.sha256")
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"output":str(dest/"coverage_report.json"),"summary":result}


def main(argv: list[str] | None = None) -> int:
    p=argparse.ArgumentParser(description="Offline venue phase coverage diagnostic")
    p.add_argument("--source-root",required=True)
    p.add_argument("--out-dir",required=True)
    p.add_argument("--symbols",default=DEFAULT_SYMBOLS)
    p.add_argument("--sessions",default=DEFAULT_DATES)
    args=p.parse_args(argv)
    try:
        result=audit(source_root=args.source_root,
                     symbols=_comma_list(args.symbols),
                     sessions=_comma_list(args.sessions),out_dir=args.out_dir)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2))
        return 0
    except VenueProbeError as exc:
        p.exit(2, f"FAIL CLOSED: {exc}\n")


if __name__=="__main__":
    raise SystemExit(main())

"""Offline audit of immutable KIS J/NX/UN snapshots; no synthetic union trades.

The only valid comparison is source/time coverage and timestamp overlap.
Vendor-integrated bars must NOT be reconstructed by summing J and NX.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import tempfile
import shutil

from .intraday_kis_venues import MARKETS, VenueProbeError


def _read_one(root: Path, symbol: str, session: str, market: str) -> tuple[dict, dict]:
    path = root / f"KIS_{market}_EXT_{symbol}_{session}"
    try:
        report = json.loads((path / "audit.json").read_bytes())
        raw_obs = (path / "minute_observations_UNVERIFIED.json").read_bytes()
        records = json.loads(raw_obs)
        if not isinstance(report, dict) or report.get("status") != (
                "KIS_INDEPENDENT_MARKET_MINUTE_INSPECTION_ONLY"):
            raise VenueProbeError("Market snapshot audit has invalid status")
        if report.get("market") != market or report.get("symbol") != symbol or report.get("session") != session:
            raise VenueProbeError("Market snapshot requested identity mismatch")
        if sha256(raw_obs).hexdigest() != report.get("source_minute_bar_sha256"):
            raise VenueProbeError("Market minute observation digest mismatch")
        digests = report.get("page_sha256")
        if not isinstance(digests, list) or len(digests) != report.get("pages"):
            raise VenueProbeError("Market minute page hash list missing")
        for n, digest in enumerate(digests):
            if sha256((path / "raw" / f"minute_{n:03}.json").read_bytes()).hexdigest() != digest:
                raise VenueProbeError("Market raw minute page hash mismatch")
        if not isinstance(records, list) or len(records) != report.get("unique_minute_labels"):
            raise VenueProbeError("Market stored minute count mismatch")
        times = [r.get("stck_cntg_hour") for r in records if isinstance(r, dict)]
        if len(times) != len(records) or len(set(times)) != len(times):
            raise VenueProbeError("Market stored time labels are duplicate/invalid")
        if report.get("usable_for_backtesting") is not False:
            raise VenueProbeError("Market source changed eligibility flag")
        day = report.get("daily_comparison")
        if not isinstance(day, dict):
            raise VenueProbeError("Official daily audit required")
        if day.get("raw_sha256") is not None:
            if sha256((path / "raw" / "daily.json").read_bytes()).hexdigest() != day["raw_sha256"]:
                raise VenueProbeError("Market daily raw digest mismatch")
        return report, {r["stck_cntg_hour"]: r for r in records}
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, VenueProbeError):
            raise
        raise VenueProbeError("Cannot verify required independent market snapshot") from exc


def audit_markets(*, root: str | Path, symbol: str, session: str,
                  out_dir: str | Path) -> dict:
    if not re.fullmatch(r"[0-9]{6}", symbol) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", session):
        raise VenueProbeError("Only six-digit ticker and YYYY-MM-DD supported")
    root_path = Path(root).expanduser().resolve()
    audits = {}
    observations = {}
    for market in ("J", "NX", "UN"):
        audits[market], observations[market] = _read_one(root_path, symbol, session, market)
    clocks = {market: set(rows) for market, rows in observations.items()}
    pairwise_clock_overlaps = {
        "J_NX": len(clocks["J"] & clocks["NX"]),
        "J_UN": len(clocks["J"] & clocks["UN"]),
        "NX_UN": len(clocks["NX"] & clocks["UN"]),
        "ALL": len(clocks["J"] & clocks["NX"] & clocks["UN"]),
    }
    # Full observed-clock matching counts, NOT evidence of duplicate executions.
    close_match = {}
    for market in ("J", "NX"):
        common = clocks[market] & clocks["UN"]
        close_match[market] = {
            "common_minute_labels": len(common),
            "same_reported_last_price_labels": sum(
                observations[market][tm]["stck_prpr"] ==
                observations["UN"][tm]["stck_prpr"] for tm in common
            ),
            "execution_venue_proven_by_price": False,
        }
    # Venue time labels are observations, not exchange trade identifiers.
    # Compare vendor-supplied minute volumes without inventing a corrected
    # integrated feed. If a source omits a time label the arithmetic uses 0
    # strictly for this diagnostic, not as evidence of zero executed shares.
    full_clock_union = clocks["J"] | clocks["NX"]
    minute_mismatches = []
    minute_sum = {
        m: sum(int(row["cntg_vol"]) for row in observations[m].values())
        for m in ("J", "NX", "UN")
    }
    for tm in sorted(clocks["UN"] | full_clock_union):
        j = int(observations["J"].get(tm, {}).get("cntg_vol", 0))
        nx = int(observations["NX"].get(tm, {}).get("cntg_vol", 0))
        un_row = observations["UN"].get(tm)
        if un_row is None:
            minute_mismatches.append({
                "label": tm, "krx": j, "nxt": nx,
                "integrated": None, "delta_un_minus_krx_nxt": None,
                "reason": "MISSING_INTEGRATED_CLOCK",
            })
            continue
        un = int(un_row["cntg_vol"])
        delta = un - j - nx
        if delta:
            minute_mismatches.append({
                "label": tm, "krx": j, "nxt": nx,
                "integrated": un, "delta_un_minus_krx_nxt": delta,
                "reason": "VENDOR_MINUTE_VOLUME_DISAGREEMENT",
            })
    minute_crosscheck = {
        "status": "MISMATCH_HOLD" if minute_mismatches else "ARITHMETIC_MATCH_ONLY",
        "all_vendor_clock_labels_covered": clocks["UN"] == full_clock_union,
        "mismatch_count": len(minute_mismatches),
        "mismatches": minute_mismatches,
        "vendor_minute_volume_sums": minute_sum,
        "minute_integrated_minus_venue_sum": minute_sum["UN"] - (
            minute_sum["J"] + minute_sum["NX"]
        ),
        "zero_for_absent_venue_clock_is_arithmetic_only": True,
        "execution_identity_verified": False,
        "approved": False,
    }
    daily_totals = {}
    daily_closes = {}
    for market in ("J", "NX", "UN"):
        d = audits[market]["daily_comparison"]
        rows = d.get("date_only_public_fields", [])
        if d.get("requested_date_matches") != 1 or not isinstance(rows, list) or len(rows) != 1:
            continue
        row = rows[0]
        if not isinstance(row, dict):
            continue
        value = row.get("acml_vol")
        close = row.get("stck_clpr")
        if (isinstance(value, str) and re.fullmatch(r"[0-9]+", value)
                and isinstance(close, str) and re.fullmatch(r"[0-9]+", close)):
            daily_totals[market] = int(value)
            daily_closes[market] = int(close)
    daily_crosscheck = {
        "status": (
            "NOT_COMPARABLE" if len(daily_totals) != 3 else
            "ARITHMETIC_MATCH_ONLY" if daily_totals["J"] + daily_totals["NX"] == daily_totals["UN"]
            else "MISMATCH_HOLD"
        ),
        "official_unadjusted_daily_volume_by_market": daily_totals,
        "official_unadjusted_daily_close_by_market": daily_closes,
        "integrated_minus_venue_volume": (
            daily_totals["UN"] - daily_totals["J"] - daily_totals["NX"]
            if len(daily_totals) == 3 else None
        ),
        "minute_minus_official_daily_volume": {
            m: minute_sum[m] - daily_totals[m] for m in daily_totals
        },
        "scope_parity_independently_verified": False,
        "approved": False,
    }
    observed_daily = {}
    for market in ("J", "NX", "UN"):
        daily = audits[market]["daily_comparison"]
        rows = daily.get("date_only_public_fields", [])
        observed_daily[market] = {
            "status": daily["status"],
            "matching_session_rows": daily.get("requested_date_matches", 0),
            "provider_original_daily_values_unverified": rows,
        }
    result = {
        "status": "KIS_J_NX_UN_SOURCE_COMPARISON_NOT_TRADE_RECONCILIATION",
        "symbol": symbol, "session": session, "market_sources": {
            market: {
                "source_sha256": audits[market]["source_minute_bar_sha256"],
                "venue_label": MARKETS[market],
                "minute_labels": audits[market]["unique_minute_labels"],
                "last_observed_label": audits[market]["sample_last_labels"][-1],
                "pagination_stop": audits[market]["pagination_stop"],
                "source_phase_clock_counts": audits[market]["clock_phase_counts_not_session_completeness"],
            }
            for market in ("J", "NX", "UN")
        },
        "same_clock_label_overlaps_not_duplicate_trades": pairwise_clock_overlaps,
        "minute_volume_arithmetic_diagnostic": minute_crosscheck,
        "official_daily_volume_arithmetic_diagnostic": daily_crosscheck,
        "source_quality_holds": [
            reason for reason, active in (
                ("INTEGRATED_MINUTE_VOLUME_MISMATCH", bool(minute_mismatches)),
                ("OFFICIAL_DAILY_VOLUME_MISMATCH",
                 daily_crosscheck["status"] == "MISMATCH_HOLD"),
                ("DAILY_SCOPE_NOT_PROVEN", True),
                ("EXECUTION_VENUE_NOT_PROVEN", True),
            ) if active
        ],
        "integrated_last_price_label_comparison": close_match,
        "official_daily_unadjusted_raw_audits": observed_daily,
        "venue_attribution": "NOT_DETERMINABLE_FROM_MINUTE_OHLCV",
        "integrated_volume_equals_sum_of_venues": "NOT_ASSUMED",
        "regular_krx_auction_vs_extended_close": "MUST_REMAIN_SEPARATE",
        "point_in_time_adjustments": "UNVERIFIED",
        "usable_for_backtesting": False,
        "usable_for_paper_or_live": False,
    }
    output_root = Path(out_dir).expanduser().resolve()
    target = output_root / f"KIS_J_NX_UN_{symbol}_{session}"
    if target.exists():
        raise VenueProbeError("Immutable market comparison already exists")
    output_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".market-compare-", dir=output_root))
    try:
        (stage / "audit.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if target.exists():
            raise VenueProbeError("Concurrent immutable market comparison")
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"output": str(target), "audit": result}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare separate KRX/NXT/integrated raw sources without trading")
    p.add_argument("--root", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--out-dir", required=True)
    args = p.parse_args(argv)
    try:
        result = audit_markets(
            root=args.root, symbol=args.symbol, session=args.session, out_dir=args.out_dir
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except VenueProbeError as exc:
        p.exit(2, f"FAIL CLOSED: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

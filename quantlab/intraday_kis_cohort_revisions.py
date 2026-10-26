"""Offline KIS historical-source revision survey across a small cohort.

Compares immutable snapshots from two independent captures. This is not
a trading-data certification: no network, credentials, daily DB mutations,
backtest, simulated order or brokerage order is performed.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

from .intraday_kis_cohort_capture import (
    DEFAULT_DATES, DEFAULT_SYMBOLS, _comma_list, plan,
)
from .intraday_kis_replay_audit import compare_replay
from .intraday_kis_venue_compare import _read_one
from .intraday_kis_venues import VenueProbeError

VENUES = ("J", "NX", "UN")


def survey(*, baseline_root: str | Path, newer_root: str | Path,
           symbols: tuple[str, ...], sessions: tuple[str, ...],
           out_dir: str | Path) -> dict:
    plan(symbols=",".join(symbols), sessions=",".join(sessions),
         markets="J,NX,UN")
    baseline = Path(baseline_root).expanduser().resolve()
    newer = Path(newer_root).expanduser().resolve()
    destination = Path(out_dir).expanduser().resolve()
    if baseline == newer:
        raise VenueProbeError("Independent capture roots must differ")
    if (destination / "cohort_revision_summary.json").exists():
        raise VenueProbeError("Immutable revision summary already exists")

    complete = []
    skipped = []
    for session in sessions:
        for symbol in symbols:
            existing = {
                rootname: [
                    m for m in VENUES if (
                        root / f"KIS_{m}_EXT_{symbol}_{session}"
                    ).is_dir()
                ]
                for rootname, root in (("baseline", baseline), ("newer", newer))
            }
            # Even partial snapshots are verified, not assumed healthy.
            for rootname, root in (("baseline", baseline), ("newer", newer)):
                for market in existing[rootname]:
                    _read_one(root, symbol, session, market)
            if any(len(existing[rootname]) != 3 for rootname in existing):
                skipped.append({
                    "symbol": symbol,
                    "session": session,
                    "status": "INCOMPLETE_CAPTURE_PAIR_NOT_COMPARABLE",
                    "baseline_missing": [
                        m for m in VENUES if m not in existing["baseline"]
                    ],
                    "newer_missing": [
                        m for m in VENUES if m not in existing["newer"]
                    ],
                })
                continue

            pair = compare_replay(
                baseline_root=baseline, replay_root=newer, symbol=symbol,
                session=session, markets=VENUES, out_dir=destination / "cases",
            )["audit"]
            revisions = {}
            for market in VENUES:
                row = pair["markets"][market]
                raw = row["raw_historical_payload_comparison"]
                revisions[market] = {
                    "changed_minute_labels": row["changed_label_count"],
                    "changed_fields": row["changed_labels"],
                    "added_labels": row["added_clock_labels"],
                    "removed_labels": row["removed_clock_labels"],
                    "normalized_observations_identical":
                        row["normal_observation_content_identical"],
                    "historical_raw_payload_status": raw["classification"],
                    "historical_minute_pages_identical":
                        raw["minute_page_historical_output2_all_identical"],
                    "official_daily_historical_rows_identical":
                        raw["historical_daily_output2_identical"],
                    "quote_header_changes_not_historical_revisions":
                        raw["classification"] ==
                        "HISTORICAL_BARS_SAME_VENDOR_OUTPUT1_CHANGED",
                    "baseline_sha256": row["baseline_minute_sha256"],
                    "newer_sha256": row["replay_minute_sha256"],
                }
            changed_count = sum(revisions[m]["changed_minute_labels"] for m in VENUES)
            full_raw_same = all(
                revisions[m]["historical_raw_payload_status"] !=
                "HISTORICAL_PAGE_OR_DAILY_PAYLOAD_DIFFERENCE_HOLD"
                for m in VENUES
            )
            complete.append({
                "symbol": symbol,
                "session": session,
                "case_status": pair["status"],
                "changed_minute_labels_total": changed_count,
                "historical_payload_changed": not full_raw_same,
                "markets": revisions,
                "passed_historical_source_stability_check": (
                    changed_count == 0 and full_raw_same
                ),
                "usable_for_backtesting": False,
            })
    changed_pairs = sum(not r["passed_historical_source_stability_check"]
                        for r in complete)
    total_changes = sum(r["changed_minute_labels_total"] for r in complete)
    summary = {
        "status": (
            "NO_COMPARABLE_CAPTURE_PAIRS" if not complete else
            "HISTORICAL_SOURCE_REVISION_HOLD" if changed_pairs else
            "SOURCES_REPEATABLE_NOT_CERTIFIED"
        ),
        "symbols": list(symbols), "sessions": list(sessions),
        "requested_pairs": len(symbols) * len(sessions),
        "complete_pairs": len(complete),
        "missing_capture_pairs": len(skipped),
        "revision_affected_pairs": changed_pairs,
        "changed_minute_labels_across_venues": total_changes,
        "revision_affected_pair_rate": (
            changed_pairs / len(complete) if complete else None
        ),
        "rate_denominator": "only source pairs with all J/NX/UN snapshots in both roots",
        "cases": complete,
        "excluded_pairs": skipped,
        "raw_vendor_payload_is_immutable": True,
        "no_market_snapshot_rewritten": True,
        "sample_too_small_for_population_claims": True,
        "output1_volatile_quote_headers_not_counted_as_history_revisions": True,
        "usable_for_backtesting": False,
        "usable_for_paper_or_live": False,
    }
    destination.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".kis-cohort-revision-", dir=destination))
    blob = (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)+"\n").encode()
    try:
        (stage / "cohort_revision_summary.json").write_bytes(blob)
        (stage / "cohort_revision_summary.sha256").write_text(
            sha256(blob).hexdigest()+"\n", encoding="utf-8"
        )
        if (destination / "cohort_revision_summary.json").exists():
            raise VenueProbeError("Concurrent immutable revision summary")
        (stage / "cohort_revision_summary.json").rename(
            destination / "cohort_revision_summary.json"
        )
        (stage / "cohort_revision_summary.sha256").rename(
            destination / "cohort_revision_summary.sha256"
        )
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"output": str(destination / "cohort_revision_summary.json"),
            "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline immutable historical 3-market capture revisions"
    )
    parser.add_argument("--baseline-root", required=True)
    parser.add_argument("--newer-root", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--sessions", default=DEFAULT_DATES)
    args = parser.parse_args(argv)
    try:
        result = survey(
            baseline_root=args.baseline_root, newer_root=args.newer_root,
            out_dir=args.out_dir, symbols=_comma_list(args.symbols),
            sessions=_comma_list(args.sessions)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except VenueProbeError as exc:
        # Diagnostics are fixed local messages; no response bodies or keys.
        parser.exit(2, f"FAIL CLOSED: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

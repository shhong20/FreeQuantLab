"""Immutable KIS historical replay-vs-baseline audit, fully offline.

Compare the *same symbol/session/venue* captured at different times, without
modifying either snapshot or ever placing orders. KRX/NXT source evidence
is only compared to UN at matching clock labels, not treated as trade IDs.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile

from .intraday_kis_venue_compare import _read_one
from .intraday_kis_venues import MARKETS, VenueProbeError

FIELDS = ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_prpr", "cntg_vol")
FOCUS = ("090700", "090800", "195900", "200000")


def _daily_raw_audit(audit: dict) -> dict:
    day = audit["daily_comparison"]
    rows = day.get("date_only_public_fields", [])
    return {
        "status": day["status"],
        "source_sha256": day.get("raw_sha256"),
        "rows": rows if isinstance(rows, list) else [],
        "day_has_matched_data": day.get("requested_date_matches") == 1,
    }


def _snapshot_comparison(*, market: str, baseline: tuple[dict, dict],
                         replay: tuple[dict, dict]) -> dict:
    a, original = baseline
    b, current = replay
    original_keys, replay_keys = set(original), set(current)
    changed = []
    for clock in sorted(original_keys & replay_keys):
        fields = {
            field: {"baseline": original[clock][field],
                    "replay": current[clock][field]}
            for field in FIELDS
            if str(original[clock][field]) != str(current[clock][field])
        }
        if fields:
            changed.append({"clock": clock, "field_changes": fields})
    orig_day, new_day = _daily_raw_audit(a), _daily_raw_audit(b)
    orig_hash, new_hash = a["source_minute_bar_sha256"], b["source_minute_bar_sha256"]
    return {
        "market": market,
        "venue": MARKETS[market],
        "baseline_minute_sha256": orig_hash,
        "replay_minute_sha256": new_hash,
        "normal_observation_content_identical": orig_hash == new_hash,
        "raw_page_hashes_identical": a["page_sha256"] == b["page_sha256"],
        "added_clock_labels": sorted(replay_keys - original_keys),
        "removed_clock_labels": sorted(original_keys - replay_keys),
        "changed_label_count": len(changed),
        "changed_labels": changed,
        "official_daily_source": {
            "baseline": orig_day, "replay": new_day,
            "same_raw_sha256": orig_day["source_sha256"] == new_day["source_sha256"],
            "same_public_rows": orig_day["rows"] == new_day["rows"],
        },
        "conclusion": (
            "OBSERVATIONS_REPRODUCED"
            if orig_hash == new_hash else
            "VENDOR_OBSERVATIONS_CHANGED_HOLD"
        ),
        "source_is_immutable": True,
    }


def _un_anomaly_against_baseline_venues(
    baseline_root: Path, symbol: str, session: str, un_baseline: dict,
    un_replay: dict,
) -> dict:
    _, krx = _read_one(baseline_root, symbol, session, "J")
    _, nxt = _read_one(baseline_root, symbol, session, "NX")
    samples = {}
    counts = {"baseline_volume_mismatch": 0, "replay_volume_mismatch": 0,
              "baseline_envelope_mismatch": 0, "replay_envelope_mismatch": 0}
    # Compare both UN snapshots against the same independently validated
    # venue baseline so that any change is attributable to the UN replay.
    for source, rows in (("baseline", un_baseline), ("replay", un_replay)):
        volume_anomalies = []
        envelope_anomalies = []
        for clock in sorted(set(krx) | set(nxt) | set(rows)):
            observed = rows.get(clock)
            venues = [d[clock] for d in (krx, nxt) if clock in d]
            if observed is None or not venues:
                continue
            expected_vol = sum(int(v["cntg_vol"]) for v in venues)
            actual_vol = int(observed["cntg_vol"])
            high = max(int(v["stck_hgpr"]) for v in venues)
            low = min(int(v["stck_lwpr"]) for v in venues)
            if actual_vol != expected_vol:
                volume_anomalies.append(clock)
            if int(observed["stck_hgpr"]) != high or int(observed["stck_lwpr"]) != low:
                envelope_anomalies.append(clock)
            if clock in FOCUS:
                samples.setdefault(clock, {})[source] = {
                    "integrated": {
                        field: observed[field] for field in FIELDS
                    },
                    "reference_krx_nxt_volume_sum": expected_vol,
                    "integrated_minus_venue_volume": actual_vol - expected_vol,
                    "reference_combined_high": high,
                    "reference_combined_low": low,
                    "integrated_matches_price_envelope": (
                        int(observed["stck_hgpr"]) == high and
                        int(observed["stck_lwpr"]) == low
                    ),
                }
        counts[f"{source}_volume_mismatch"] = len(volume_anomalies)
        counts[f"{source}_envelope_mismatch"] = len(envelope_anomalies)
        counts[f"{source}_volume_mismatch_labels"] = volume_anomalies
        counts[f"{source}_envelope_mismatch_labels"] = envelope_anomalies
    return {
        "status": "SAME_BASELINE_J_NX_NOT_CERTIFIED_TRADES",
        "krx_nxt_reference_snapshots": "BASELINE_ONLY",
        "counts": counts,
        "focus_labels": samples,
        "vendor_timestamp_semantics_verified": False,
        "replication_not_evidence_of_correctness": True,
    }


def compare_replay(*, baseline_root: str | Path, replay_root: str | Path,
                   symbol: str, session: str, markets: tuple[str, ...] = ("UN",),
                   out_dir: str | Path) -> dict:
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
        raise VenueProbeError("Invalid replay symbol")
    if not isinstance(session, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", session):
        raise VenueProbeError("Invalid replay session")
    if not markets or len(set(markets)) != len(markets) or any(m not in MARKETS for m in markets):
        raise VenueProbeError("Replay markets must be unique J/NX/UN codes")
    base = Path(baseline_root).expanduser().resolve()
    newer = Path(replay_root).expanduser().resolve()
    if base == newer:
        raise VenueProbeError("Baseline and replay roots must differ")
    comparisons = {}
    for market in markets:
        baseline = _read_one(base, symbol, session, market)
        replay = _read_one(newer, symbol, session, market)
        comparisons[market] = _snapshot_comparison(
            market=market, baseline=baseline, replay=replay
        )
        if market == "UN":
            comparisons["UN"]["baseline_venue_crosscheck"] = (
                _un_anomaly_against_baseline_venues(
                    base, symbol, session, baseline[1], replay[1]
                )
            )
    status = (
        "REPLAY_OBSERVATIONS_REPRODUCED"
        if all(comparisons[m]["normal_observation_content_identical"] for m in markets)
        else "REPLAY_OBSERVATIONS_CHANGED_HOLD"
    )
    result = {
        "status": status,
        "session": session,
        "symbol": symbol,
        "markets": comparisons,
        "raw_market_sources_preserved": True,
        "replayed_market_data_proven_correct": False,
        "market_clock_semantics_verified": False,
        "usable_for_backtesting": False,
        "usable_for_paper_or_live": False,
    }
    target_root = Path(out_dir).expanduser().resolve()
    target = target_root / f"KIS_REPLAY_{'-'.join(markets)}_{symbol}_{session}"
    if target.exists():
        raise VenueProbeError("Immutable replay comparison already exists")
    target_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".kis-replay-audit-", dir=target_root))
    try:
        blob = (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        (stage / "audit.json").write_bytes(blob)
        (stage / "audit.sha256").write_text(sha256(blob).hexdigest() + "\n", encoding="utf-8")
        if target.exists():
            raise VenueProbeError("Concurrent replay comparison already exists")
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"output": str(target), "audit": result}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Offline immutable KIS same-day replay audit")
    p.add_argument("--baseline-root", required=True)
    p.add_argument("--replay-root", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--markets", default="UN")
    p.add_argument("--out-dir", required=True)
    args = p.parse_args(argv)
    try:
        result = compare_replay(
            baseline_root=args.baseline_root, replay_root=args.replay_root,
            symbol=args.symbol, session=args.session,
            markets=tuple(args.markets.split(",")), out_dir=args.out_dir,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except VenueProbeError as exc:
        p.exit(2, f"FAIL CLOSED: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

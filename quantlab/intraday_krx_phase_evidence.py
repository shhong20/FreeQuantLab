"""Read-only KRX session-evidence reconciliation for unrepresented trading phases.

Independent phase statistics are accepted only with locally preserved raw source
bytes and SHA-256. A matching arithmetic total NEVER licenses reconstruction
of one-minute bars or automatically certifies exchange/vendor scope.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile

from .intraday_kis_cohort_capture import DEFAULT_DATES, DEFAULT_SYMBOLS, _comma_list, plan
from .intraday_kis_venue_compare import _read_one
from .intraday_kis_venues import VenueProbeError


PHASES = (
    "KRX_PREOPEN_FIXED_CLOSE",
    "KRX_POSTCLOSE_FIXED_CLOSE",
    "KRX_PREOPEN_BLOCK_BASKET",
    "KRX_POSTCLOSE_BLOCK_BASKET",
)
SUPPORTED_AGENCIES = ("KRX", "KIS", "KOSCOM")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}")
MAX_MANIFEST_ROWS = 60


def _read_external_evidence(evidence_root: Path | None,
                            wanted: set[tuple[str, str]]) -> dict:
    if evidence_root is None:
        return {}
    root = evidence_root.expanduser().resolve()
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise VenueProbeError("External evidence manifest does not exist")
    try:
        blob = manifest.read_bytes()
        data = json.loads(blob)
    except (OSError, ValueError, UnicodeError) as exc:
        raise VenueProbeError("Cannot parse external evidence manifest") from exc
    if not isinstance(data, dict) or data.get("format_version") != 1:
        raise VenueProbeError("Unsupported external evidence manifest version")
    entries = data.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_MANIFEST_ROWS:
        raise VenueProbeError("Invalid phase evidence entry count")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise VenueProbeError("Invalid phase evidence row")
        symbol, session, phase = (
            entry.get("symbol"), entry.get("session"), entry.get("phase")
        )
        if (not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol)
                or not isinstance(session, str)
                or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", session)
                or phase not in PHASES):
            raise VenueProbeError("Invalid evidence case or phase")
        if (symbol, session) not in wanted:
            raise VenueProbeError("Evidence not in requested cohort")
        if (symbol, session, phase) in result:
            raise VenueProbeError("Duplicate evidence case and trading phase")
        quantity = entry.get("volume")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise VenueProbeError("Evidence volume must be a nonnegative integer")
        provider = entry.get("provider")
        if provider not in SUPPORTED_AGENCIES:
            raise VenueProbeError("Evidence provider must be named KRX/KIS/KOSCOM")
        rel = entry.get("source_file")
        if not isinstance(rel, str) or not rel or len(rel) > 180:
            raise VenueProbeError("Invalid evidence source filename")
        source = (root / rel).resolve()
        if root not in source.parents or not source.is_file():
            raise VenueProbeError("Evidence raw source must be below external evidence root")
        expected = entry.get("source_sha256")
        if not isinstance(expected, str) or not HASH_PATTERN.fullmatch(expected):
            raise VenueProbeError("Evidence source SHA-256 invalid")
        try:
            actual = sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            raise VenueProbeError("Cannot read source evidence") from exc
        if expected != actual:
            raise VenueProbeError("External evidence raw hash mismatch")
        record_id = entry.get("source_record_id")
        if not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,90}", record_id):
            raise VenueProbeError("Evidence source record identifier invalid")
        # The manifest's claims of provider and phase are NOT independently
        # authenticated just because the referenced raw bytes match a hash.
        result[symbol, session, phase] = {
            "phase": phase,
            "volume": quantity,
            "declared_provider": provider,
            "raw_sha256": expected,
            "source_record_id": record_id,
            "source_bytes_integrity_verified": True,
            "official_trade_phase_semantics_authenticated": False,
            "provider_authenticity_independently_verified": False,
        }
    return result


def assess(*, cohort_root: str | Path, out_dir: str | Path,
           symbols: tuple[str, ...], sessions: tuple[str, ...],
           evidence_root: str | Path | None = None) -> dict:
    plan(symbols=",".join(symbols), sessions=",".join(sessions), markets="J")
    target = Path(out_dir).expanduser().resolve()
    if (target / "phase_evidence_audit.json").exists():
        raise VenueProbeError("Immutable phase evidence audit already exists")
    root = Path(cohort_root).expanduser().resolve()
    wanted = {(symbol, session) for symbol in symbols for session in sessions}
    external = _read_external_evidence(
        Path(evidence_root) if evidence_root is not None else None, wanted
    )
    cases = []
    missing = []
    for session in sessions:
        for symbol in symbols:
            source_dir = root / f"KIS_J_EXT_{symbol}_{session}"
            if not source_dir.is_dir():
                missing.append({
                    "symbol": symbol, "session": session,
                    "reason": "KRX_HISTORICAL_SOURCE_NOT_CAPTURED",
                })
                continue
            audit, observed = _read_one(root, symbol, session, "J")
            rows = audit["daily_comparison"].get("date_only_public_fields", [])
            if audit["daily_comparison"].get("requested_date_matches") != 1 or len(rows) != 1:
                raise VenueProbeError("KRX official daily source match missing")
            field = rows[0].get("acml_vol")
            if not isinstance(field, str) or not field.isdecimal():
                raise VenueProbeError("KRX official daily volume missing")
            daily = int(field)
            minute = sum(int(row["cntg_vol"]) for row in observed.values())
            gap = daily-minute
            evidence = [external.get((symbol, session, phase)) for phase in PHASES]
            entries = [r for r in evidence if r is not None]
            fully_covered = len(entries) == len(PHASES)
            partial_volume = sum(r["volume"] for r in entries)
            cases.append({
                "symbol": symbol, "session": session,
                "source_minute_sha256": audit["source_minute_bar_sha256"],
                "official_krx_daily_volume": daily,
                "observed_krx_minute_volume": minute,
                "unexplained_official_minus_observed": gap,
                "evidence_by_trading_phase": {
                    phase: external.get((symbol, session, phase))
                    for phase in PHASES
                },
                "phases_with_evidence": len(entries),
                "phases_without_evidence": [
                    phase for phase in PHASES
                    if (symbol, session, phase) not in external
                ],
                "submitted_phase_volume_sum_partial": partial_volume,
                "all_four_candidate_phase_records_present": fully_covered,
                "difference_after_four_submitted_phase_totals": (
                    gap - partial_volume if fully_covered else None
                ),
                "arithmetic_match_is_not_proof_of_scope": True,
                "other_possible_trades_or_corrections_unverified": True,
                "vendor_phase_semantics_independently_certified": False,
                "backtest_allowed": False,
            })
    result = {
        "status": "EXTERNAL_TRADING_PHASE_PROVENANCE_UNVERIFIED_HOLD",
        "cases": cases,
        "missing_krx_cases": missing,
        "compared_krx_cases": len(cases),
        "source_record_count": len(external),
        "candidate_phases": list(PHASES),
        "phase_source_coverage_is_not_trade_completeness": True,
        "provided_raw_hash_integrity_is_not_vendor_authentication": True,
        "no_missing_volume_as_zero": True,
        "no_bar_synthesis_or_vendor_data_rewrite": True,
        "usable_for_backtesting": False,
        "usable_for_paper_or_live": False,
    }
    target.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".phase-evidence-", dir=target))
    try:
        contents = (json.dumps(result, indent=2, sort_keys=True,
                               ensure_ascii=False) + "\n").encode()
        (staged / "phase_evidence_audit.json").write_bytes(contents)
        (staged / "phase_evidence_audit.sha256").write_text(
            sha256(contents).hexdigest() + "\n", encoding="utf-8"
        )
        if (target / "phase_evidence_audit.json").exists():
            raise VenueProbeError("Concurrent phase evidence report")
        (staged / "phase_evidence_audit.json").rename(
            target / "phase_evidence_audit.json"
        )
        (staged / "phase_evidence_audit.sha256").rename(
            target / "phase_evidence_audit.sha256"
        )
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return {"output": str(target / "phase_evidence_audit.json"),
            "audit": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline KRX phase evidence audit")
    parser.add_argument("--cohort-root", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--sessions", default=DEFAULT_DATES)
    parser.add_argument("--evidence-root", default=None,
                        help="Optional directory containing manifest.json and original files")
    args = parser.parse_args(argv)
    try:
        result = assess(
            cohort_root=args.cohort_root, out_dir=args.out_dir,
            symbols=_comma_list(args.symbols),
            sessions=_comma_list(args.sessions),
            evidence_root=args.evidence_root
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except VenueProbeError:
        parser.exit(2, "FAIL CLOSED: VenueProbeError (source integrity or contract)\n")


if __name__ == "__main__":
    raise SystemExit(main())

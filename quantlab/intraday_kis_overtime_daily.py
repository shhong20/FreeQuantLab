"""Independent read-only KIS overtime DAILY historical research source.

Official KIS example FHPST02320000 returns its *latest 30 trading records*
without a user-specified historical date. Unlike day-selected minute history,
this is a rolling-window observation, not a backfill guarantee.

No original minute bars, production market data, or trade account changed.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Callable

import requests

from .data import _kis_token
from .intraday_kis_cohort_capture import (
    DEFAULT_DATES, DEFAULT_SYMBOLS, _comma_list, plan,
)
from .intraday_kis_venues import VenueProbeError, _get, _decode_response

URL = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-overtimeprice"
TR_ID = "FHPST02320000"
MARKET = "J"
SOURCE = "KIS_FHPST02320000_ROLLING_LAST_30"


def _validate_inputs(symbols: tuple[str, ...], sessions: tuple[str, ...]) -> None:
    # Reuse preexisting input bounds and path traversal defense.
    plan(symbols=",".join(symbols), sessions=",".join(sessions), markets="J")


def _safe_number(value: object) -> int | None:
    return int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) else None


def parse_response(raw: bytes, *, symbol: str,
                   sessions: tuple[str, ...]) -> dict:
    body = _decode_response(raw, market="J", is_daily=True)
    data = body.get("output2")
    if not isinstance(data, list) or len(data) > 30:
        raise VenueProbeError("Invalid last-30 overtime historical rows")
    records: dict[str, dict] = {}
    for row in data:
        if not isinstance(row, dict):
            raise VenueProbeError("Overtime historical row must be an object")
        day = row.get("stck_bsop_date")
        if (not isinstance(day, str) or
                not re.fullmatch(r"[0-9]{8}", day)):
            raise VenueProbeError("Overtime row lacks business date")
        try:
            date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:]}")
        except ValueError as exc:
            raise VenueProbeError("Overtime business date invalid") from exc
        if day in records:
            if row != records[day]:
                raise VenueProbeError("Conflicting overtime date duplicate")
            raise VenueProbeError("Duplicate overtime business date")
        records[day] = row

    matches = []
    missing = []
    for session in sessions:
        day = session.replace("-", "")
        row = records.get(day)
        if row is None:
            missing.append(session)
            continue
        volume = _safe_number(row.get("ovtm_untp_vol"))
        matches.append({
            "session": session,
            "date_is_directly_returned": True,
            "overtime_single_price_volume_reported": volume,
            "raw_volume_field": "ovtm_untp_vol",
            "field_scope": "OVERTIME_UNIT_PRICE_VENDOR_LABEL_UNVERIFIED_VS_KRX_GAP",
            "missing_or_invalid_volume_field": volume is None,
        })
    return {
        "status": "OVERTIME_RECENT_DAY_SOURCE_ONLY_NOT_KRX_GAP_EXPLANATION",
        "symbol": symbol,
        "requested_sessions": list(sessions),
        "market": MARKET,
        "tr_id": TR_ID,
        "endpoint_scope": "ROLLING_LAST_30_NO_HISTORICAL_DATE_QUERY",
        "response_records": len(records),
        "returned_date_bounds": [
            min(records) if records else None,
            max(records) if records else None,
        ],
        "matched_requested_days": matches,
        "requested_days_not_in_response": missing,
        "historical_range_guaranteed": False,
        "krx_fixed_price_preopen_vs_after_close_separated": False,
        "time_zone_and_phase_volume_semantics_verified": False,
        "vendor_revision_semantics_verified": False,
        "usable_to_reconcile_krx_daily_minute_gap": False,
        "usable_for_backtesting": False,
        "orders_enabled": False,
    }


def capture_one(*, symbol: str, sessions: tuple[str, ...], out_dir: str | Path,
                key: str, secret: str, token: str,
                getter: Callable = requests.get) -> dict:
    _validate_inputs((symbol,), sessions)
    if not all(isinstance(v, str) and v for v in (key, secret, token)):
        raise VenueProbeError("KIS credentials required in local execution")
    root = Path(out_dir).expanduser().resolve()
    output = root / f"KIS_OVERTIME_DAILY_J_{symbol}"
    if output.exists():
        raise VenueProbeError("Immutable overtime source already exists")

    raw = _get(
        URL, params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
        },
        key=key, secret=secret, token=token, tr_id=TR_ID, getter=getter,
    )
    report = parse_response(raw, symbol=symbol, sessions=sessions)
    report["raw_sha256"] = sha256(raw).hexdigest()
    report["requested_at_utc"] = datetime.now(timezone.utc).isoformat()
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".overtime-daily-", dir=root))
    try:
        (stage / "raw.json").write_bytes(raw)
        (stage / "audit.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)+"\n",
            encoding="utf-8",
        )
        if output.exists():
            raise VenueProbeError("Concurrent overtime source already exists")
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"output": str(output), "audit": report}


def _read_snapshot(root: Path, symbol: str, sessions: tuple[str, ...]) -> dict:
    folder = root / f"KIS_OVERTIME_DAILY_J_{symbol}"
    try:
        raw = (folder / "raw.json").read_bytes()
        report = json.loads((folder / "audit.json").read_bytes())
        if not isinstance(report, dict) or report.get("symbol") != symbol or (
            report.get("status") !=
            "OVERTIME_RECENT_DAY_SOURCE_ONLY_NOT_KRX_GAP_EXPLANATION"
        ):
            raise VenueProbeError("Overtime source identity/status invalid")
        if sha256(raw).hexdigest() != report.get("raw_sha256"):
            raise VenueProbeError("Overtime vendor response SHA mismatch")
        originally_requested = report.get("requested_sessions")
        if (not isinstance(originally_requested, list)
                or not originally_requested
                or any(not isinstance(x, str) for x in originally_requested)
                or any(x not in originally_requested for x in sessions)):
            raise VenueProbeError("Overtime snapshot was not collected for requested sessions")
        regenerated = parse_response(
            raw, symbol=symbol, sessions=tuple(originally_requested)
        )
        for field in (
            "matched_requested_days", "requested_days_not_in_response",
            "response_records", "returned_date_bounds"
        ):
            if report.get(field) != regenerated[field]:
                raise VenueProbeError("Overtime audit and raw response differ")
        return report
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, VenueProbeError):
            raise
        raise VenueProbeError("Cannot inspect immutable overtime source") from exc


def bridge(*, overtime_root: str | Path, cohort_root: str | Path,
           symbols: tuple[str, ...], sessions: tuple[str, ...],
           out_dir: str | Path) -> dict:
    _validate_inputs(symbols, sessions)
    over = Path(overtime_root).expanduser().resolve()
    cohort = Path(cohort_root).expanduser().resolve()
    destination = Path(out_dir).expanduser().resolve()
    if (destination / "bridge.json").exists():
        raise VenueProbeError("Immutable overtime comparison exists")
    from .intraday_kis_venue_compare import _read_one

    records = []
    for symbol in symbols:
        report = _read_snapshot(over, symbol, sessions)
        day_map = {
            v["session"]: v
            for v in report["matched_requested_days"]
        }
        for session in sessions:
            # A date not present in the last-30 provider response does not
            # become zero volume or a different session's volume.
            ref, obs = _read_one(cohort, symbol, session, "J")
            daily = ref["daily_comparison"]
            selected = daily.get("date_only_public_fields", [])
            if (daily.get("requested_date_matches") != 1 or
                    not isinstance(selected, list) or len(selected) != 1):
                gap = None
            else:
                daily_vol = _safe_number(selected[0].get("acml_vol"))
                gap = (daily_vol - sum(int(r["cntg_vol"]) for r in obs.values())
                       if daily_vol is not None else None)
            source_row = day_map.get(session)
            after_vol = (source_row["overtime_single_price_volume_reported"]
                         if source_row is not None else None)
            records.append({
                "symbol": symbol, "session": session,
                "krx_daily_minus_observed_minute_volume": gap,
                "overtime_reported_untp_volume": after_vol,
                "comparison_difference_gap_minus_untp_volume": (
                    gap - after_vol
                    if gap is not None and after_vol is not None else None
                ),
                "overtime_date_in_vendor_response": source_row is not None,
                "same_venue_and_time_scope_proven": False,
                "equal_values_do_not_prove_trade_scope": True,
                "disposition": "NO_AUTOMATIC_VOLUME_REPAIR_OR_BACKTEST",
            })
    result = {
        "status": "OVERTIME_VOLUME_SCOPE_COMPARISON_UNVERIFIED",
        "cases": records,
        "compared_with_overtime_value": sum(
            x["comparison_difference_gap_minus_untp_volume"] is not None
            for x in records
        ),
        "missing_recent_rolling_days": sum(
            not x["overtime_date_in_vendor_response"] for x in records
        ),
        "all_krx_volume_gaps_attributed": False,
        "no_trade_phase_volume_imputation": True,
        "usable_for_backtesting": False,
        "usable_for_paper_or_live": False,
    }
    destination.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".overtime-bridge-", dir=destination))
    try:
        blob = (json.dumps(result, ensure_ascii=False,
                           indent=2, sort_keys=True)+"\n").encode("utf-8")
        (stage / "bridge.json").write_bytes(blob)
        (stage / "bridge.sha256").write_text(sha256(blob).hexdigest()+"\n")
        if (destination / "bridge.json").exists():
            raise VenueProbeError("Concurrent bridge comparison")
        (stage / "bridge.json").rename(destination / "bridge.json")
        (stage / "bridge.sha256").rename(destination / "bridge.sha256")
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"output": str(destination / "bridge.json"), "audit": result}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="KIS rolling last-30 overtime history; dry run by default"
    )
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--sessions", default=DEFAULT_DATES)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)
    try:
        symbols, sessions = _comma_list(args.symbols), _comma_list(args.sessions)
        _validate_inputs(symbols, sessions)
        if not args.execute:
            print(json.dumps({
                "status": "DRY_RUN_NO_API_CALL",
                "symbols": list(symbols), "sessions": list(sessions),
                "api": SOURCE, "requests": len(symbols),
                "historical_date_in_request": False,
                "orders_enabled": False,
            }, ensure_ascii=False, sort_keys=True))
            return 0
        resolved_root = Path(args.out_dir).expanduser().resolve()
        already_done = []
        for symbol in symbols:
            target = resolved_root / f"KIS_OVERTIME_DAILY_J_{symbol}"
            if target.exists():
                _read_snapshot(resolved_root, symbol, sessions)
                already_done.append(symbol)
        # Avoid an unnecessary OAuth token request if nothing needs capture.
        need_fetch = any(symbol not in already_done for symbol in symbols)
        if need_fetch:
            key, secret = os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", "")
            if not key or not secret:
                raise VenueProbeError("Missing local KIS credentials")
            token = _kis_token()
        else:
            key = secret = token = ""
        out = []
        requested = 0
        for symbol in symbols:
            if symbol in already_done:
                out.append({"symbol": symbol, "status": "EXISTING_VERIFIED"})
                continue
            # Pace *new HTTP requests*, not source filenames. This also
            # handles the case where the first symbol is already persisted.
            if requested:
                time.sleep(2.0)
            requested += 1
            new = capture_one(
                symbol=symbol, sessions=sessions, out_dir=args.out_dir,
                key=key, secret=secret, token=token
            )
            out.append({
                "symbol": symbol, "status": "CAPTURED",
                "matched_dates": len(new["audit"]["matched_requested_days"]),
                "missing_dates": new["audit"]["requested_days_not_in_response"],
            })
        print(json.dumps({"status":"SOURCE_ONLY_NO_RECONCILIATION",
                          "newly_requested_symbols":requested,
                          "results":out},ensure_ascii=False,sort_keys=True))
        return 0
    except Exception as exc:
        # No sensitive response body, Authorization header or account fields.
        error_type = type(exc).__name__
        if error_type not in ("VenueProbeError", "DataError"):
            error_type = "UnexpectedError"
        p.exit(2, f"FAIL CLOSED: {error_type}\n")


def compare_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Offline compare overtime daily to KIS KRX gap")
    p.add_argument("--overtime-root", required=True)
    p.add_argument("--cohort-root", required=True)
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--sessions", default=DEFAULT_DATES)
    p.add_argument("--out-dir", required=True)
    args = p.parse_args(argv)
    try:
        out = bridge(overtime_root=args.overtime_root, cohort_root=args.cohort_root,
                     symbols=_comma_list(args.symbols),
                     sessions=_comma_list(args.sessions), out_dir=args.out_dir)
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except VenueProbeError as exc:
        p.exit(2, f"FAIL CLOSED: {type(exc).__name__}\n")


if __name__ == "__main__":
    raise SystemExit(main())

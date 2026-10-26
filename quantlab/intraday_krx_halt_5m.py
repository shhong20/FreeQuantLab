"""KRX 5-minute candidate slots with explicit halt/data-gap execution barriers.

ALL outputs are offline research artifacts. Missing one-minute OHLCV is null,
never zero-filled, forward-filled, resampled across a halt, or passed to the
legacy strategy engine. Media-reported circuit breaker times remain unverified
until an exchange event feed and provider minute timestamp semantics agree.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

from .intraday_krx_5m_preview import CLOCKS, FIELDS, validate_offline_plan
from .intraday_kis_cohort_capture import DEFAULT_DATES, DEFAULT_SYMBOLS, _comma_list
from .intraday_kis_venue_compare import _read_one
from .intraday_kis_venues import VenueProbeError

# Reported KOSPI market-wide circuit breaker activation times, *not*
# authenticated KRX machine-event records. First-stage rule of 20m suspended
# continuous trading followed by 10m call auction is a conservative
# potential no-continuous-execution window, NOT a validated execution clock.
REPORTED_KOSPI_CB = {
    "2026-07-28": {
        "activation_kst": "10:13:43",
        "source": "https://v.daum.net/v/20260728101810442",
    },
    "2026-07-29": {
        "activation_kst": "12:32:32",
        "source": "https://v.daum.net/v/20260729125639247",
    },
    "2026-10-15": {
        "activation_kst": "12:38:12",
        "source": "https://ir.newspim.com/news/view/20261015000760",
    },
}
MINUTES_PER_BUCKET = 5
EXPECTED_BUCKETS = 76


def _encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _event(session: str) -> dict | None:
    record = REPORTED_KOSPI_CB.get(session)
    if record is None:
        return None
    begin = datetime.fromisoformat(f"{session}T{record['activation_kst']}+09:00")
    end = begin + timedelta(minutes=30)
    return {
        "activation_kst": begin.isoformat(),
        "conservative_continuous_order_blackout_until_kst": end.isoformat(),
        "source_url": record["source"],
        "source_quality": "PUBLIC_REPORT_NOT_EXCHANGE_EXECUTION_EVENT_FEED",
        "20min_halt_plus_10min_call_auction_is_provisional": True,
    }


def _minute(row: dict, expected_day: str) -> dict:
    if row.get("stck_bsop_date") != expected_day.replace("-", ""):
        raise VenueProbeError("Wrong-day 1m observation")
    try:
        values = {key: int(row[value]) for key, value in FIELDS.items()}
    except (KeyError, ValueError, OverflowError, TypeError) as exc:
        raise VenueProbeError("Invalid 1m OHLCV") from exc
    if (min(values["open"], values["high"], values["low"], values["close"]) <= 0
            or values["volume"] < 0
            or values["high"] < max(values["open"], values["low"], values["close"])
            or values["low"] > min(values["open"], values["high"], values["close"])):
        raise VenueProbeError("Contradictory 1m OHLCV")
    return values


def _window_overlaps(session: str, start_label: str, event: dict | None) -> bool:
    if not event:
        return False
    start = datetime.fromisoformat(
        f"{session}T{start_label[:2]}:{start_label[2:4]}:00+09:00"
    )
    stop = start + timedelta(minutes=MINUTES_PER_BUCKET)
    return (
        start < datetime.fromisoformat(
            event["conservative_continuous_order_blackout_until_kst"])
        and stop > datetime.fromisoformat(event["activation_kst"])
    )


def analyze_day(*, source_root: Path, symbol: str, session: str) -> dict:
    audit, observed = _read_one(source_root, symbol, session, "J")
    ev = _event(session)
    closing_label_present = "153000" in observed
    closing_candidates = sorted(
        x for x in observed if "152000" <= x <= "154000"
    )
    five_minute_slots = []
    slot_counts: Counter[str] = Counter()
    incomplete_count = 0
    minutes_missing = 0
    for offset in range(0, len(CLOCKS), MINUTES_PER_BUCKET):
        expected = CLOCKS[offset:offset + MINUTES_PER_BUCKET]
        actual = [x for x in expected if x in observed]
        missing = [x for x in expected if x not in observed]
        overlaps = _window_overlaps(session, expected[0], ev)
        if missing:
            incomplete_count += 1
            minutes_missing += len(missing)
            # A reported CB is a possible explanation only for missing
            # clocks whose *minute interval* overlaps the reported 30m window.
            related = [
                x for x in missing if _window_overlaps(session, x, ev)
                # _window_overlaps uses a five-minute interval; conservative
                # overlap classifications are advisory and never certify a CB.
            ]
            if related and len(related) == len(missing):
                state = "INCOMPLETE_CB_CORRELATED_UNVERIFIED"
            elif related:
                state = "INCOMPLETE_CB_AND_UNEXPLAINED_GAPS"
            else:
                state = "INCOMPLETE_SOURCE_GAP_UNEXPLAINED"
            # No partial-volume bar or invented price is allowed.
            candle = None
        else:
            m = [_minute(observed[x], session) for x in expected]
            candle = {
                "open": m[0]["open"],
                "high": max(x["high"] for x in m),
                "low": min(x["low"] for x in m),
                "close": m[-1]["close"],
                "volume": sum(x["volume"] for x in m),
            }
            state = (
                "FULL_OHLCV_CB_BLACKOUT_OVERLAP_NO_CONTINUOUS_ORDER_MODEL"
                if overlaps else "FULL_OBSERVED_5M_PROVISIONAL"
            )
        label = expected[0]
        slot_counts[state] += 1
        five_minute_slots.append({
            "timestamp": f"{session}T{label[:2]}:{label[2:4]}:00+09:00",
            "source_one_minute_clock_labels": list(actual),
            "missing_one_minute_clock_labels": missing,
            "source_observed_count": len(actual),
            "market_reported_cb_window_overlap": overlaps,
            "state": state,
            "ohlcv": candle,
            "continuous_order_execution_allowed": False,
            "is_complete_and_verified_for_strategy": False,
        })
    if len(five_minute_slots) != EXPECTED_BUCKETS:
        raise VenueProbeError("Expected exactly 76 5-minute regular clock slots")
    if sum(s["source_observed_count"] for s in five_minute_slots) + minutes_missing != 380:
        raise VenueProbeError("Observed and missing 1m clocks do not reconcile")
    observed_regular_sum = sum(
        _minute(observed[x], session)["volume"] for x in CLOCKS if x in observed
    )
    complete_bucket_sum = sum(
        s["ohlcv"]["volume"] for s in five_minute_slots if s["ohlcv"] is not None
    )
    if complete_bucket_sum > observed_regular_sum:
        raise VenueProbeError("Complete 5m bucket volume exceeds observed 1m sum")
    complete_regular = minutes_missing == 0
    if ev:
        disposition = "ENTIRE_DAY_STRATEGY_PNL_BLOCKED_MARKET_HALT_RISK"
    elif not complete_regular:
        disposition = "ENTIRE_DAY_STRATEGY_PNL_BLOCKED_UNKNOWN_GAPS"
    elif not closing_label_present:
        disposition = "ENTIRE_DAY_STRATEGY_PNL_BLOCKED_END_MARKER_UNVERIFIED"
    else:
        disposition = "PROVISIONAL_COMPLETE_DAY_VENDOR_SEMANTICS_HOLD"
    return {
        "symbol": symbol,
        "session": session,
        "source_market": "KRX_J",
        "source_minute_sha256": audit["source_minute_bar_sha256"],
        "reported_cb": ev,
        "complete_regular_session_clock": complete_regular,
        "observed_regular_minute_count": 380 - minutes_missing,
        "missing_regular_minute_count": minutes_missing,
        "complete_five_minute_bucket_count": EXPECTED_BUCKETS - incomplete_count,
        "incomplete_five_minute_bucket_count": incomplete_count,
        "slot_state_counts": dict(slot_counts),
        "closing_153000_label_present": closing_label_present,
        "observed_closing_clock_candidates": closing_candidates,
        "closing_time_semantics_verified": False,
        "observed_regular_minute_volume_sum": observed_regular_sum,
        "fully_observed_5m_bucket_volume_sum": complete_bucket_sum,
        "remainder_is_observed_minutes_in_incomplete_buckets_not_imputed":
            observed_regular_sum - complete_bucket_sum,
        "day_disposition": disposition,
        "no_imputed_bar_prices_or_volumes": True,
        "vendor_timestamp_start_end_contract_verified": False,
        "paper_or_live_enabled": False,
        "backtest_pnl_enabled": False,
        "slots": five_minute_slots,
    }


def build(*, source_root: str | Path, out_dir: str | Path,
          symbols: tuple[str, ...], sessions: tuple[str, ...]) -> dict:
    validate_offline_plan(symbols, sessions)
    root = Path(source_root).expanduser().resolve()
    output = Path(out_dir).expanduser().resolve()
    if output.exists():
        raise VenueProbeError("Immutable halt research directory already exists")
    rows = []
    stats: Counter[str] = Counter()
    by_symbol = {}
    for symbol in symbols:
        daily = []
        for session in sessions:
            if not (root / f"KIS_J_EXT_{symbol}_{session}").is_dir():
                raise VenueProbeError("KRX source missing; no zero-day imputation")
            day = analyze_day(source_root=root, symbol=symbol, session=session)
            daily.append(day)
            stats[day["day_disposition"]] += 1
            rows.append({
                key: value for key, value in day.items()
                if key != "slots"
            })
        by_symbol[symbol] = daily
    summary = {
        "status": "HALT_AWARE_5M_CANDIDATES_RESEARCH_ONLY_NO_PNL",
        "source": "SHA_VERIFIED_KIS_KRX_J_ORIGINAL_MINUTES",
        "symbols": list(symbols),
        "sessions": list(sessions),
        "total_stock_days": len(rows),
        "total_5m_clock_slots": len(rows) * EXPECTED_BUCKETS,
        "day_dispositions": dict(stats),
        "day_records_without_slots": rows,
        "market_reports_not_authenticated_KRX_event_feed": True,
        "halt_days_not_silently_dropped_from_dataset": True,
        "no_zero_or_last_price_fill_for_missing_clock": True,
        "broker_execution_simulation_performed": False,
        "strategy_selection_performed": False,
        "walkforward_returns_calculated": False,
        "usable_for_backtesting": False,
        "usable_for_paper_or_live": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".krx-halt-5m-", dir=output.parent))
    try:
        for symbol in symbols:
            content = _encoded(by_symbol[symbol])
            (staging / f"{symbol}_halt_5m_UNVERIFIED.json").write_bytes(content)
            (staging / f"{symbol}_halt_5m_UNVERIFIED.sha256").write_text(
                sha256(content).hexdigest() + "\n", encoding="utf-8"
            )
        blob = _encoded(summary)
        (staging / "halt_5m_summary.json").write_bytes(blob)
        (staging / "halt_5m_summary.sha256").write_text(
            sha256(blob).hexdigest() + "\n", encoding="utf-8"
        )
        if output.exists():
            raise VenueProbeError("Concurrent immutable halt dataset")
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"summary": summary, "output": str(output / "halt_5m_summary.json")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline 5m halt-aware research, NEVER PnL")
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--sessions", default=DEFAULT_DATES)
    args = parser.parse_args(argv)
    try:
        result = build(
            source_root=args.source_root, out_dir=args.out_dir,
            symbols=_comma_list(args.symbols), sessions=_comma_list(args.sessions)
        )
        summary = result["summary"]
        print(json.dumps({
            "output": result["output"],
            "status": summary["status"],
            "total_stock_days": summary["total_stock_days"],
            "total_5m_clock_slots": summary["total_5m_clock_slots"],
            "day_dispositions": summary["day_dispositions"],
            "usable_for_backtesting": False,
        }, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except VenueProbeError:
        parser.exit(2, "FAIL CLOSED: VenueProbeError (immutable raw or session)\n")


if __name__ == "__main__":
    raise SystemExit(main())

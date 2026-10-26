"""Independent KIS J/NX/UN historical venue snapshots (read-only).

Unlike the existing single-day KRX closing-auction probe this module captures
venue-specific extended-hours data without assuming every minute traded,
that KRX/NXT/UN contain the same sessions, or that UN equals J + NX.
No order endpoints or operating ledger writes; all raw pages are immutable.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
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

from .data import DataError, _kis_token
from .intraday_kis_probe import _request_day, KISMinuteError, URL, TR_ID, FIELDS

MARKETS = {"J": "KRX", "NX": "NXT", "UN": "INTEGRATED_UNATTRIBUTED"}
DAILY_URL = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
DAILY_TR_ID = "FHKST03010100"
MAX_PAGES = 15
MAX_BYTES = 3_000_000


class VenueProbeError(ValueError):
    pass


def _safe_provider_code(payload: dict) -> str:
    value = payload.get("msg_cd")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) else "UNKNOWN"


def _decode_response(raw: bytes, *, market: str, is_daily: bool = False) -> dict:
    if not isinstance(raw, bytes) or not (0 < len(raw) <= MAX_BYTES):
        raise VenueProbeError("Empty or oversized KIS historical response")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise VenueProbeError("KIS response was not JSON") from exc
    if not isinstance(body, dict):
        raise VenueProbeError("KIS response is not a JSON object")
    if str(body.get("rt_cd")) != "0":
        # Do not log provider msg1, HTTP body or account-related metadata.
        raise VenueProbeError(
            f"KIS {market} {'daily' if is_daily else 'minute'} request denied "
            f"(msg_cd={_safe_provider_code(body)})"
        )
    return body


def _get(url: str, *, params: dict, key: str, secret: str,
         token: str, tr_id: str, getter: Callable) -> bytes:
    # Keys and tokens exist only in HTTPS headers, never on disk/in logs.
    headers = {
        "authorization": f"Bearer {token}", "appkey": key,
        "appsecret": secret, "tr_id": tr_id, "custtype": "P"
    }
    try:
        response = getter(url, params=params, headers=headers, timeout=25)
        response.raise_for_status()
        return response.content
    except requests.RequestException as exc:
        raise VenueProbeError(
            f"KIS read-only request failed ({type(exc).__name__})"
        ) from None


def phase(market: str, label: str) -> str:
    """Time-window classification, NOT proof that a trade occurred."""
    minute = int(label[:2]) * 60 + int(label[2:4])
    if market == "J":
        if 540 <= minute < 920:
            return "krx_regular_continuous"
        if minute == 930:
            return "krx_closing_auction_marker"
        if 960 <= minute <= 1200:
            return "krx_after_market_candidate"
        return "krx_other_or_unconfirmed"
    if market == "NX":
        if 480 <= minute < 530:
            return "nxt_pre_market"
        if 540 <= minute < 920:
            return "nxt_main_market_candidate"
        if 940 <= minute <= 1200:
            return "nxt_after_market_candidate"
        return "nxt_other_or_break"
    # An integrated bar does not expose execution venue. Time-only grouping.
    if 480 <= minute < 540:
        return "integrated_pre_0900_unknown_venue"
    if 540 <= minute <= 930:
        return "integrated_0900_1530_unknown_venue"
    if 930 < minute <= 1200:
        return "integrated_after_1530_unknown_venue"
    return "integrated_other_unknown_venue"


def collect_minute(*, symbol: str, session: str, market: str,
                   key: str, secret: str, token: str,
                   getter: Callable = requests.get,
                   now: datetime | None = None,
                   pause: Callable[[float], None] = time.sleep
                   ) -> tuple[list[bytes], list[dict], dict]:
    if market not in MARKETS:
        raise VenueProbeError("Only J/NX/UN markets are allowed")
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
        raise VenueProbeError("Six-digit KRX stock ticker required")
    _request_day(session, now or datetime.now(timezone.utc))
    if not all(isinstance(v, str) and v.strip() for v in (key, secret, token)):
        raise VenueProbeError("Authenticated read-only KIS keys/token required")
    ymd = session.replace("-", "")
    cursor = "200000"
    seen_cursors: set[str] = set()
    raw_pages: list[bytes] = []
    records: dict[str, dict] = {}
    overlapping = 0
    excluded_previous = 0
    stop = "page_budget"
    # Caller-limited pacing is deliberate, unlike uncontrolled parallel API calls.
    for page in range(MAX_PAGES):
        if cursor in seen_cursors:
            raise VenueProbeError("Repeated KIS venue page cursor")
        seen_cursors.add(cursor)
        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": cursor,
            "FID_INPUT_DATE_1": ymd,
            "FID_PW_DATA_INCU_YN": "N",
            "FID_FAKE_TICK_INCU_YN": "",
        }
        if page:
            pause(0.2)
        raw = _get(URL, params=params, key=key, secret=secret,
                   token=token, tr_id=TR_ID, getter=getter)
        body = _decode_response(raw, market=market)
        rows = body.get("output2")
        if not isinstance(rows, list) or len(rows) > 120:
            raise VenueProbeError("Invalid KIS venue minute rows")
        raw_pages.append(raw)
        if not rows:
            stop = "empty_page"
            break
        this_times: list[str] = []
        crossed = False
        for item in rows:
            if not isinstance(item, dict) or any(k not in item for k in FIELDS):
                raise VenueProbeError("Missing KIS venue minute OHLCV fields")
            day = str(item["stck_bsop_date"])
            if day != ymd:
                if re.fullmatch(r"[0-9]{8}", day) and day < ymd:
                    excluded_previous += 1
                    crossed = True
                    continue
                raise VenueProbeError("Future or invalid venue session")
            stamp = str(item["stck_cntg_hour"])
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]", stamp):
                raise VenueProbeError("Invalid KIS venue minute timestamp")
            # Prices/volumes are parsed only for validation, raw strings
            # and raw page bytes remain unchanged for audit.
            try:
                values = [int(item[k]) for k in (
                    "stck_oprc", "stck_hgpr", "stck_lwpr", "stck_prpr", "cntg_vol"
                )]
            except (ValueError, TypeError, OverflowError) as exc:
                raise VenueProbeError("Invalid KIS venue OHLCV") from exc
            op, hi, lo, cl, volume = values
            if min(op, hi, lo, cl) <= 0 or volume < 0 or hi < max(op, lo, cl) or lo > min(op, hi, cl):
                raise VenueProbeError("Contradictory KIS venue minute OHLCV")
            canonical = {k: item[k] for k in FIELDS}
            if stamp in records:
                if records[stamp] != canonical:
                    raise VenueProbeError("Conflicting venue duplicate minute")
                overlapping += 1
            records[stamp] = canonical
            this_times.append(stamp)
        if crossed:
            if not records:
                raise VenueProbeError("Requested date unavailable in venue history")
            stop = "prior_day_boundary"
            break
        if not this_times:
            raise VenueProbeError("Venue page did not contain requested date")
        oldest = min(this_times)
        if oldest >= cursor:
            if len(rows) < 120:
                # A thinly traded market may legitimately have only the
                # exact requested time label; keep it as an incomplete audit.
                stop = "short_page_no_earlier_labels"
                break
            raise VenueProbeError("Venue pages have no backward progress")
        if oldest <= "080000":
            stop = "reached_0800"
            break
        if len(rows) < 120:
            stop = "short_page_unproven_earlier_history"
            break
        cursor = oldest
    if not records:
        raise VenueProbeError("No matching venue minute bars")
    if stop == "page_budget":
        # Don't silently publish a potentially truncated market history.
        raise VenueProbeError("Venue history exceeded maximum pages")
    counts = dict(sorted(Counter(phase(market, t) for t in records).items()))
    audit = {
        "status": "KIS_INDEPENDENT_MARKET_MINUTE_INSPECTION_ONLY",
        "market": market, "venue": MARKETS[market],
        "symbol": symbol, "session": session,
        "source": "KIS_FHKST03010230",
        "requested_end_time": "200000",
        "pages": len(raw_pages),
        "page_sha256": [sha256(p).hexdigest() for p in raw_pages],
        "unique_minute_labels": len(records),
        "clock_phase_counts_not_session_completeness": counts,
        "sample_first_labels": sorted(records)[:3],
        "sample_last_labels": sorted(records)[-3:],
        "duplicate_identical_minute_labels": overlapping,
        "excluded_previous_session_rows": excluded_previous,
        "pagination_stop": stop,
        "supports_query_documented": True,
        "vendor_has_historical_data_for_market": "RESPONSE_OBSERVED",
        "minute_timestamp_start_end_semantics": "UNVERIFIED",
        "per_minute_volume_definition": "UNVERIFIED",
        "integrated_disjoint_sum_of_venues": "UNVERIFIED",
        "missing_minute_imputation": False,
        "automatic_5min_conversion": False,
        "usable_for_backtesting": False,
        "orders_enabled": False,
    }
    return raw_pages, [records[t] for t in sorted(records)], audit


def collect_daily(*, symbol: str, session: str, market: str,
                  key: str, secret: str, token: str,
                  getter: Callable = requests.get) -> tuple[bytes, dict]:
    ymd = session.replace("-", "")
    params = {
        "FID_COND_MRKT_DIV_CODE": market,
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": ymd,
        "FID_INPUT_DATE_2": ymd,
        "FID_PERIOD_DIV_CODE": "D",
        # Per official example: 1 = original/unadjusted (for date comparison).
        "FID_ORG_ADJ_PRC": "1",
    }
    raw = _get(DAILY_URL, params=params, key=key, secret=secret,
               token=token, tr_id=DAILY_TR_ID, getter=getter)
    body = _decode_response(raw, market=market, is_daily=True)
    rows = body.get("output2")
    if not isinstance(rows, list) or len(rows) > 100:
        raise VenueProbeError("Invalid venue official daily bars")
    match = [r for r in rows if isinstance(r, dict) and str(r.get("stck_bsop_date")) == ymd]
    if any(not isinstance(r, dict) for r in rows):
        raise VenueProbeError("Invalid official daily row")
    # Do not infer session from latest nonmatching date; immutable raw is retained.
    selected = [{
        "stck_bsop_date": r.get("stck_bsop_date"),
        "stck_oprc": r.get("stck_oprc"),
        "stck_hgpr": r.get("stck_hgpr"),
        "stck_lwpr": r.get("stck_lwpr"),
        "stck_clpr": r.get("stck_clpr"),
        "acml_vol": r.get("acml_vol"),
    } for r in match]
    return raw, {
        "status": (
            "KIS_VENUE_DAILY_RAW_UNVERIFIED"
            if selected else "KIS_VENUE_DAILY_NO_MATCHING_SESSION"
        ),
        "source": "KIS_FHKST03010100",
        "market": market, "venue": MARKETS[market],
        "session": session, "symbol": symbol, "unadjusted_request": True,
        "requested_date_matches": len(selected),
        "date_only_public_fields": selected,
        "raw_sha256": sha256(raw).hexdigest(),
        "same_scope_as_minute_source": "UNVERIFIED",
        "usable_for_backtesting": False,
    }


def capture(*, symbol: str, session: str, market: str,
            out_dir: str | Path, key: str, secret: str, token: str,
            getter: Callable = requests.get,
            now: datetime | None = None,
            pause: Callable[[float], None] = time.sleep) -> dict:
    if market not in MARKETS:
        raise VenueProbeError("Invalid KIS market")
    _request_day(session, now or datetime.now(timezone.utc))
    if not re.fullmatch(r"[0-9]{6}", symbol):
        raise VenueProbeError("Invalid KIS ticker")
    root = Path(out_dir).expanduser().resolve()
    folder = root / f"KIS_{market}_EXT_{symbol}_{session}"
    if folder.exists():
        raise VenueProbeError("Independent venue snapshot already exists")
    raw, bars, report = collect_minute(
        symbol=symbol, session=session, market=market, key=key, secret=secret,
        token=token, getter=getter, now=now, pause=pause
    )
    # Daily query failure should not discard successful minute raw data.
    # Only static, allowed exception class/code is placed in the audit.
    daily_raw: bytes | None = None
    try:
        daily_raw, daily_audit = collect_daily(
            symbol=symbol, session=session, market=market,
            key=key, secret=secret, token=token, getter=getter
        )
    except VenueProbeError as exc:
        message = str(exc)
        safe_code = re.search(r"msg_cd=([A-Za-z0-9_-]{1,32})", message)
        daily_audit = {
            "status": "DAILY_NOT_COLLECTED",
            "error_class": "VenueProbeError",
            "safe_provider_code": safe_code.group(1) if safe_code else "UNKNOWN",
            "same_scope_as_minute_source": "UNVERIFIED",
            "usable_for_backtesting": False,
        }
    report["daily_comparison"] = daily_audit
    observations_blob = (
        json.dumps(bars, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    report["source_minute_bar_sha256"] = sha256(observations_blob).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".venue-probe-", dir=root))
    try:
        (stage / "raw").mkdir()
        for i, contents in enumerate(raw):
            (stage / "raw" / f"minute_{i:03d}.json").write_bytes(contents)
        if daily_raw is not None:
            (stage / "raw" / "daily.json").write_bytes(daily_raw)
        (stage / "minute_observations_UNVERIFIED.json").write_bytes(
            observations_blob
        )
        (stage / "audit.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if folder.exists():
            raise VenueProbeError("Concurrent venue snapshot already exists")
        stage.rename(folder)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"snapshot": str(folder), "audit": report}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Independent KIS KRX/NXT/UN venue source snapshots")
    p.add_argument("--symbol", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--markets", default="J,NX,UN", help="J,NX,UN comma list, not merged")
    args = p.parse_args(argv)
    markets = args.markets.split(",")
    if not markets or len(set(markets)) != len(markets) or any(m not in MARKETS for m in markets):
        p.exit(2, "FAIL CLOSED: markets must be unique J,NX,UN codes\n")
    try:
        key, secret = os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", "")
        if not key or not secret:
            raise VenueProbeError("KIS environment variables are missing")
        token = _kis_token()
        for market in markets:
            # A failed market should not destroy independent successful runs.
            # Fail with a code immediately: operator can diagnose per venue.
            got = capture(symbol=args.symbol, session=args.session,
                          market=market, out_dir=args.out_dir,
                          key=key, secret=secret, token=token)
            audit = got["audit"]
            print(json.dumps({
                "market": market, "status": audit["status"],
                "snapshot": got["snapshot"],
                "labels": audit["unique_minute_labels"],
                "phase_counts": audit["clock_phase_counts_not_session_completeness"],
                "daily_status": audit["daily_comparison"]["status"],
                "backtest_allowed": False,
            }, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except (VenueProbeError, KISMinuteError, DataError) as exc:
        # These are static, sanitized diagnostic classes (no credentials).
        p.exit(2, f"FAIL CLOSED: {type(exc).__name__}: {exc}\n")
    except (OSError, requests.RequestException) as exc:
        p.exit(2, f"FAIL CLOSED: {type(exc).__name__}\n")


if __name__ == "__main__":
    raise SystemExit(main())

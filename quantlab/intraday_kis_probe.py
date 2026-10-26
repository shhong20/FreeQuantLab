"""One-day KIS minute-bar provenance probe, strictly research-only.

KIS stck_cntg_hour / cntg_vol semantics must be independently verified
before aggregating to start-labeled 5m candles. No backtests or orders.
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
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from .data import _kis_token


class KISMinuteError(ValueError):
    pass


KST = ZoneInfo("Asia/Seoul")
URL = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
TR_ID = "FHKST03010230"
FIELDS = ("stck_bsop_date", "stck_cntg_hour", "stck_oprc", "stck_hgpr",
          "stck_lwpr", "stck_prpr", "cntg_vol")
MAX_PAGES = 12
MAX_BYTES = 3_000_000


def _request_day(day: str, now: datetime) -> str:
    if now.tzinfo is None:
        raise KISMinuteError("Timezone-aware now required")
    try:
        d = date.fromisoformat(day)
    except ValueError as exc:
        raise KISMinuteError("Session must be YYYY-MM-DD") from exc
    if d >= now.astimezone(KST).date():
        raise KISMinuteError("Only fully past KRX dates can be probed")
    if (now.astimezone(KST).date() - d).days > 366:
        raise KISMinuteError("KIS historical minute retention is at most about one year")
    return d.strftime("%Y%m%d")


def probe_kis(*, symbol: str, session: str, token: str,
              getter: Callable = requests.get, now: datetime | None = None
              ) -> tuple[list[bytes], list[dict], dict]:
    """Download validated KIS pages, preserving data before choosing bar semantics."""
    now = now or datetime.now(timezone.utc)
    ymd = _request_day(session, now)
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol, re.ASCII):
        raise KISMinuteError("KIS ticker must be exactly six ASCII digits")
    if not token or not token.strip():
        raise KISMinuteError("KIS read-only token required")
    headers = {"authorization": f"Bearer {token}",
               "appkey": os.getenv("KIS_APP_KEY", ""),
               "appsecret": os.getenv("KIS_APP_SECRET", ""),
               "tr_id": TR_ID, "custtype": "P"}
    if not headers["appkey"] or not headers["appsecret"]:
        raise KISMinuteError("KIS_APP_KEY and KIS_APP_SECRET required")
    cursor = "153000"
    seen_cursors: set[str] = set()
    snapshots: list[bytes] = []
    records: dict[str, dict] = {}
    pagination_stop = "page_limit"
    duplicate_same = 0
    for _ in range(MAX_PAGES):
        if cursor in seen_cursors:
            raise KISMinuteError("KIS pagination cursor repeated")
        seen_cursors.add(cursor)
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": cursor,
            "FID_INPUT_DATE_1": ymd,
            "FID_PW_DATA_INCU_YN": "Y",
            "FID_FAKE_TICK_INCU_YN": "",
        }
        try:
            r = getter(URL, params=params, headers=headers, timeout=25)
            r.raise_for_status()
            raw = r.content
        except requests.RequestException as exc:
            raise KISMinuteError(
                f"KIS historical minute request failed ({type(exc).__name__})"
            ) from None
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
            raise KISMinuteError("KIS response missing/oversized")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise KISMinuteError("Invalid KIS minute JSON") from exc
        if not isinstance(body, dict) or str(body.get("rt_cd")) != "0":
            raise KISMinuteError("KIS provider rejected minute-bar request")
        data = body.get("output2")
        if not isinstance(data, list) or len(data) > 120:
            raise KISMinuteError("KIS output2 is not a valid <=120 row page")
        if not data:
            pagination_stop = "empty_page"
            snapshots.append(raw)
            break
        page_times = []
        for item in data:
            if not isinstance(item, dict) or any(k not in item for k in FIELDS):
                raise KISMinuteError("KIS raw minute fields missing")
            if str(item["stck_bsop_date"]) != ymd:
                raise KISMinuteError("Provider returned different session")
            tm = str(item["stck_cntg_hour"])
            if not re.fullmatch(r"[0-2][0-9][0-5][0-9][0-5][0-9]", tm):
                raise KISMinuteError("Malformed minute time label")
            hour = int(tm[:2])
            if hour > 23:
                raise KISMinuteError("Invalid minute hour")
            prices = []
            try:
                prices = [float(item[k]) for k in (
                    "stck_oprc", "stck_hgpr", "stck_lwpr", "stck_prpr"
                )]
                volume = float(item["cntg_vol"])
            except (TypeError, ValueError) as exc:
                raise KISMinuteError("Invalid OHLCV value") from exc
            if any(not __import__("math").isfinite(x) or x <= 0 for x in prices):
                raise KISMinuteError("Invalid KIS minute price")
            if (not __import__("math").isfinite(volume) or volume < 0
                or prices[1] < max(prices[0], prices[2], prices[3])
                or prices[2] > min(prices[0], prices[1], prices[3])):
                raise KISMinuteError("Contradictory KIS minute bar")
            canonical = {k: item[k] for k in FIELDS}
            if tm in records:
                if records[tm] != canonical:
                    raise KISMinuteError("Conflicting duplicate minute value across pages")
                duplicate_same += 1
            records[tm] = canonical
            page_times.append(tm)
        snapshots.append(raw)
        oldest = min(page_times)
        if oldest >= cursor:
            raise KISMinuteError("KIS minute pagination made no backward progress")
        if oldest <= "090000":
            pagination_stop = "reached_open"
            break
        if len(data) < 120:
            pagination_stop = "short_page_unproven_prior_history"
            break
        cursor = oldest
    else:
        raise KISMinuteError("KIS minute pagination exceeded page budget")
    if not records:
        raise KISMinuteError("No KIS minute rows")
    ordered = [records[t] for t in sorted(records)]
    labels = set(records)
    expected = set(f"{m//60:02d}{m%60:02d}00" for m in range(9*60, 15*60+20))
    present = expected & labels
    auction = sorted(t for t in labels if "152000" <= t <= "153000")
    report = {
        "status": "KIS_ONE_DAY_MINUTE_PROVENANCE_ONLY_NO_5MIN_BACKTEST",
        "session": session, "symbol": symbol, "tr_id": TR_ID,
        "raw_pages": len(snapshots), "raw_sha256": [sha256(x).hexdigest() for x in snapshots],
        "unique_minute_labels": len(ordered), "benign_page_boundary_duplicates": duplicate_same,
        "continuous_reference_0900_1519_minute_labels": len(present),
        "continuous_reference_expected_max": 380,
        "missing_reference_labels": len(expected - labels),
        "closing_auction_time_labels": auction,
        "pagination_stop": pagination_stop,
        "sample_first_labels": sorted(labels)[:5],
        "sample_last_labels": sorted(labels)[-5:],
        "timestamp_label_semantics": "UNVERIFIED_START_OR_END",
        "cntg_vol_semantics": "UNVERIFIED_MINUTE_VOLUME",
        "krx_closing_auction_model": "UNVERIFIED",
        "automatic_5min_conversion": False,
        "backtest_or_orders_allowed": False,
    }
    return snapshots, ordered, report


def capture_kis(*, symbol: str, session: str, out_dir: str | Path,
                token: str | None = None,
                getter: Callable = requests.get,
                now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    _request_day(session, now)
    if not re.fullmatch(r"[0-9]{6}", symbol, re.ASCII):
        raise KISMinuteError("Invalid KRX ticker")
    folder = Path(out_dir).expanduser().resolve() / f"KIS_{symbol}_{session}"
    if folder.exists():
        raise KISMinuteError("Immutable KIS minute snapshot already exists")
    raw, rows, audit = probe_kis(
        symbol=symbol, session=session, token=token or _kis_token(),
        getter=getter, now=now,
    )
    folder.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".kis-minute-", dir=folder.parent))
    try:
        (staging / "raw").mkdir()
        for i, payload in enumerate(raw):
            (staging/"raw"/f"page_{i:03d}.json").write_bytes(payload)
        data = (json.dumps(rows, ensure_ascii=False, sort_keys=True,
                           indent=2)+"\n").encode()
        (staging/"minute_observations_unverified.json").write_bytes(data)
        audit["observations_sha256"] = sha256(data).hexdigest()
        (staging/"audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2)+"\n",
            encoding="utf-8",
        )
        if folder.exists():
            raise KISMinuteError("Concurrent KIS immutable snapshot conflict")
        staging.rename(folder)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"snapshot": str(folder), "audit": audit}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="KIS historical one-day minute provenance, no trading")
    p.add_argument("--symbol", required=True, help="Six-digit KRX ticker")
    p.add_argument("--session", required=True, help="YYYY-MM-DD, fully past market day")
    p.add_argument("--out-dir", required=True)
    args = p.parse_args(argv)
    try:
        result = capture_kis(
            symbol=args.symbol, session=args.session, out_dir=args.out_dir
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KISMinuteError, ValueError, requests.RequestException, OSError) as exc:
        p.exit(2, f"FAIL CLOSED (minute provenance only): {type(exc).__name__}\n")


if __name__ == "__main__":
    raise SystemExit(main())

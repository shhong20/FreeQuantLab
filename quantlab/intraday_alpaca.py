"""Authenticated historical Alpaca 5-minute collection: research-only, never orders.

The default feed is SIP (consolidated historical tape), not sparse IEX.
Raw responses, completeness exclusions, and unmodified CSV have SHA-256
provenance. Never stores keys or modifies existing stock/crypto ledgers.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .intraday_research import IntradayDataError, evaluate, validate


class SourceError(ValueError):
    pass


HOST = "https://data.alpaca.markets"
NY = ZoneInfo("America/New_York")
FIELDS = ("timestamp", "open", "high", "low", "close", "volume")
PAGE_LIMIT = 10000
MAX_PAGES = 250
MAX_PAGE_BYTES = 6_000_000


def _dates(start: str, end: str, now: datetime) -> tuple[date, date]:
    if now.tzinfo is None:
        raise SourceError("Timezone-aware now required")
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise SourceError("Use ISO start/end dates YYYY-MM-DD") from exc
    if a > b or (b - a).days > 1096:
        raise SourceError("Invalid 0..1096 day historical range")
    if b >= now.astimezone(NY).date():
        raise SourceError("Only fully past US market dates are allowed")
    if (b-a).days < 90:
        raise SourceError("Use at least 90 calendar days for >=60 completed sessions")
    return a, b


def _symbol(symbol: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", symbol, flags=re.ASCII):
        raise SourceError("Invalid ASCII US equity ticker")
    return symbol


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not re.search(r"(Z|[+-]\d{2}:\d{2})$", value):
        raise SourceError("Alpaca bar timestamp must be timezone-aware")
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError("Invalid Alpaca bar timestamp") from exc
    if ts.tzinfo is None:
        raise SourceError("Missing source timestamp timezone")
    return ts.astimezone(timezone.utc)


def fetch_alpaca(*, symbol: str, start: str, end: str,
                 key: str, secret: str,
                 feed: str = "sip", now: datetime | None = None,
                 getter: Callable = requests.get
                 ) -> tuple[pd.DataFrame, list[bytes], dict]:
    """Pure network acquisition. Auth only in headers; never persisted/logged.

    IEX is opt-in for *coverage diagnostics only*. Its lower volume coverage
    can invalidate RVOL and breakout rules; evaluation is therefore blocked.
    """
    now = now or datetime.now(timezone.utc)
    a, b = _dates(start, end, now)
    _symbol(symbol)
    if feed not in {"sip", "iex"}:
        raise SourceError("Only sip/iex US stock feeds supported")
    if not key or not secret or not key.strip() or not secret.strip():
        raise SourceError("Alpaca APCA_API_KEY_ID and APCA_API_SECRET_KEY required")
    params = {
        "timeframe": "5Min",
        "start": a.isoformat() + "T00:00:00Z",
        "end": (b + timedelta(days=1)).isoformat() + "T00:00:00Z",
        "limit": PAGE_LIMIT,
        "feed": feed,
        "adjustment": "raw",
        "sort": "asc",
    }
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    next_token: str | None = None
    seen_tokens: set[str] = set()
    raw_pages: list[bytes] = []
    records: list[dict] = []
    previous_ts: datetime | None = None
    for _ in range(MAX_PAGES):
        page_params = dict(params)
        if next_token:
            page_params["page_token"] = next_token
        # Errors deliberately omit response URLs, headers and body, avoiding
        # accidental logging of user credentials or account metadata.
        try:
            response = getter(
                f"{HOST}/v2/stocks/{symbol}/bars",
                params=page_params, headers=headers, timeout=30,
            )
            response.raise_for_status()
            raw = response.content
        except requests.RequestException as exc:
            raise SourceError(
                f"Alpaca read-only historical request rejected ({type(exc).__name__})"
            ) from None
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PAGE_BYTES:
            raise SourceError("Alpaca raw page missing or oversized")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise SourceError("Invalid Alpaca JSON page") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("bars"), list):
            raise SourceError("Unexpected Alpaca single-symbol bars schema")
        if payload.get("symbol") not in (None, symbol):
            raise SourceError("Source returned wrong stock symbol")
        bars = payload["bars"]
        if len(bars) > PAGE_LIMIT:
            raise SourceError("Too many Alpaca bars in one page")
        # Guard against missing pages and repeated timestamp ranges.
        for item in bars:
            if not isinstance(item, dict) or not all(
                k in item for k in ("t", "o", "h", "l", "c", "v")
            ):
                raise SourceError("Alpaca bar has missing OHLCV fields")
            ts = _timestamp(item["t"])
            if previous_ts is not None and ts <= previous_ts:
                raise SourceError("Duplicate or out-of-order bar across pages")
            if ts.date() < a - timedelta(days=1) or ts.date() > b + timedelta(days=1):
                raise SourceError("Provider bar outside requested UTC guard range")
            previous_ts = ts
            records.append({
                "timestamp": ts.isoformat(), "open": item["o"], "high": item["h"],
                "low": item["l"], "close": item["c"], "volume": item["v"],
            })
        raw_pages.append(raw)
        token = payload.get("next_page_token")
        if token in (None, ""):
            break
        if not isinstance(token, str) or len(token) > 1024 or token in seen_tokens:
            raise SourceError("Invalid or repeated Alpaca pagination token")
        seen_tokens.add(token)
        next_token = token
    else:
        raise SourceError("Alpaca exceeded MAX_PAGES; no incomplete history published")
    if not records:
        raise SourceError("No historical bars returned")
    return pd.DataFrame.from_records(records, columns=FIELDS), raw_pages, {
        "provider": "alpaca", "symbol": symbol, "feed": feed,
        "timeframe": "5Min", "adjustment": "raw",
        "start": start, "end": end, "pages": len(raw_pages),
        "raw_bars": len(records),
    }


def extract_complete_sessions(raw: pd.DataFrame, start: str, end: str
                              ) -> tuple[pd.DataFrame, dict]:
    """Never fill gaps. Drop entire incomplete sessions and disclose each reason."""
    if raw.empty or not set(FIELDS).issubset(raw.columns):
        raise SourceError("No valid raw 5-minute dataset")
    try:
        ts = pd.to_datetime(raw["timestamp"], utc=True, errors="raise")
    except (ValueError, TypeError) as exc:
        raise SourceError("Malformed source timestamps") from exc
    local = ts.dt.tz_convert(NY)
    sessions = local.dt.strftime("%Y-%m-%d")
    times = local.dt.strftime("%H:%M")
    cutoff = date.fromisoformat(start)
    final = date.fromisoformat(end)
    market_minutes = [f"{(570 + 5*i)//60:02d}:{(570 + 5*i)%60:02d}"
                      for i in range(78)]
    selected: list[pd.DataFrame] = []
    rejected: dict[str, str] = {}
    pre_post = 0
    all_sessions = sorted(set(sessions))
    for session in all_sessions:
        if not cutoff <= date.fromisoformat(session) <= final:
            rejected[session] = "outside_requested_market_date"
            continue
        day_idx = raw.index[sessions == session]
        on = day_idx[times.loc[day_idx].isin(market_minutes)]
        pre_post += len(day_idx) - len(on)
        observed = times.loc[on].tolist()
        if observed != market_minutes:
            rejected[session] = (
                f"missing_or_noncontiguous_regular_bars_{len(observed)}_of_78"
            )
            continue
        selected.append(raw.loc[on, list(FIELDS)].copy())
    if not selected:
        raise SourceError("No complete 78-bar sessions; no partial fills permitted")
    clean = pd.concat(selected, ignore_index=True)
    try:
        # Shared strict contract also validates OHLC, time zone, ordering and
        # minimum 60 complete historical sessions.
        clean = validate(clean, "US_EQUITY")
    except IntradayDataError as exc:
        raise SourceError(f"Shared bar quality gate failed: {exc}") from exc
    return clean, {
        "observed_vendor_sessions": len(all_sessions),
        "complete_sessions": int(clean["session"].nunique()),
        "excluded_sessions": rejected,
        "excluded_pre_or_post_market_bars": pre_post,
        "no_synthetic_missing_bars": True,
    }


def _json_bytes(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True,
                       indent=2, allow_nan=False) + "\n").encode("utf-8")


def capture(*, symbol: str, start: str, end: str, out_dir: str | Path,
            key: str, secret: str, feed: str = "sip",
            now: datetime | None = None,
            getter: Callable = requests.get) -> dict:
    """Immutable new snapshot, local-only. No persisted token, order or ledger."""
    ticker = _symbol(symbol)
    folder = Path(out_dir).expanduser().resolve() / f"{ticker}_{start}_{end}_{feed}"
    if folder.exists():
        raise SourceError("Immutable research snapshot already exists; no overwrite")
    bars, pages, info = fetch_alpaca(
        symbol=ticker, start=start, end=end, key=key, secret=secret,
        feed=feed, now=now, getter=getter,
    )
    clean, quality = extract_complete_sessions(bars, start, end)
    csv_bytes = clean[list(FIELDS)].to_csv(index=False).encode("utf-8")
    csv_sha = sha256(csv_bytes).hexdigest()
    manifest = {
        **info, **quality,
        "market": "US_EQUITY",
        "dataset_sha256": csv_sha,
        "raw_page_sha256": [sha256(p).hexdigest() for p in pages],
        "feed_coverage_gate": (
            "CONSOLIDATED_SIP_HISTORICAL_CANDIDATE"
            if feed == "sip" else "IEX_PARTIAL_MARKET_NOT_PERFORMANCE_ELIGIBLE"
        ),
        "corporate_actions_issuer_verified": False,
        "orders_enabled": False,
    }
    if feed == "sip":
        result = evaluate(clean, market="US_EQUITY",
                          source_sha256=csv_sha,
                          corporate_actions_reviewed=False)
        manifest["evaluation_status"] = result["status"]
        report = _json_bytes(result)
    else:
        manifest["evaluation_status"] = "BLOCKED_IEC_PARTIAL_MARKET"
        report = _json_bytes({
            "status": "BLOCKED_IEX_PARTIAL_MARKET_NOT_COMPARABLE",
            "reason": "Only IEX exchange trades; ORB RVOL/price filters require SIP verification",
        })
    folder.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".intraday-staging-", dir=folder.parent))
    try:
        (stage / "raw").mkdir()
        for index, raw in enumerate(pages):
            (stage / "raw" / f"alpaca_page_{index:04}.json").write_bytes(raw)
        (stage / "bars_5m.csv").write_bytes(csv_bytes)
        (stage / "report.json").write_bytes(report)
        (stage / "manifest.json").write_bytes(_json_bytes(manifest))
        if folder.exists():
            raise SourceError("Concurrent snapshot conflict; refusing overwrite")
        stage.rename(folder)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"status": "IMMUTABLE_INTRADAY_RESEARCH_SNAPSHOT_ONLY",
            "path": str(folder), "manifest": manifest,
            "strategy_report": str(folder / "report.json")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only historical Alpaca SIP 5m stock research")
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True, help="YYYY-MM-DD (US market dates)")
    p.add_argument("--end", required=True, help="YYYY-MM-DD (fully in the past)")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--feed", choices=["sip", "iex"], default="sip")
    args = p.parse_args(argv)
    try:
        r = capture(
            symbol=args.symbol, start=args.start, end=args.end,
            out_dir=args.out_dir, feed=args.feed,
            key=os.getenv("APCA_API_KEY_ID", ""),
            secret=os.getenv("APCA_API_SECRET_KEY", ""),
        )
        print(_json_bytes(r).decode())
        return 0
    except (SourceError, OSError, ValueError) as exc:
        p.exit(2, f"FAIL CLOSED: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

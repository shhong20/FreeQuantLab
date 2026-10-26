"""Bounded, resumable KIS J/NX/UN source collection for 2-stock 3-day research.

NO strategy, exchange order, paper trading or operational database writes.
CLI is a dry run unless explicitly invoked with --execute on user's WSL.
Every vendor response is independently preserved by existing capture().
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import time
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from .data import _kis_token
from .intraday_kis_venues import MARKETS, VenueProbeError, capture
from .intraday_kis_venue_compare import _read_one

DEFAULT_SYMBOLS = "005930,000660"
DEFAULT_DATES = "2026-10-21,2026-10-22,2026-10-23"
DEFAULT_MARKETS = "J,NX,UN"
MAX_TASKS = 30
ALLOWED_SYMBOLS = 3
ALLOWED_SESSIONS = 5


def _comma_list(raw: str) -> tuple[str, ...]:
    values = tuple(raw.split(","))
    if not values or len(set(values)) != len(values) or any(not v for v in values):
        raise VenueProbeError("Plan inputs must be unique, nonempty comma-separated values")
    return values


def plan(*, symbols: str, sessions: str, markets: str,
         today: date | None = None) -> list[dict]:
    syms = _comma_list(symbols)
    days = _comma_list(sessions)
    venues = _comma_list(markets)
    if not (1 <= len(syms) <= ALLOWED_SYMBOLS):
        raise VenueProbeError("Research plan permits 1 to 3 stock symbols")
    if not (1 <= len(days) <= ALLOWED_SESSIONS):
        raise VenueProbeError("Research plan permits 1 to 5 dates")
    if len(venues) > 3 or any(v not in MARKETS for v in venues):
        raise VenueProbeError("Research plan permits unique J,NX,UN venues only")
    if any(not re.fullmatch(r"[0-9]{6}", v) for v in syms):
        raise VenueProbeError("Six-digit domestic stock symbols required")
    if any(not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v) for v in days):
        raise VenueProbeError("Research dates must be YYYY-MM-DD")
    try:
        dates = [date.fromisoformat(v) for v in days]
    except ValueError as exc:
        raise VenueProbeError("Invalid ISO research date") from exc
    limit = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    if any(d >= limit or d.weekday() >= 5 for d in dates):
        raise VenueProbeError("Research requires past weekdays only (exchange holidays separately checked)")
    if len(syms) * len(days) * len(venues) > MAX_TASKS:
        raise VenueProbeError("Research plan exceeds 30 independently bounded tasks")
    # Iterate by date first, then stock, then venue for auditable coverage.
    return [
        {"symbol": symbol, "session": day, "market": market}
        for day in days for symbol in syms for market in venues
    ]


class PacedGetter:
    """Serial KIS GETs, global monotonic minimum spacing (incl. daily request)."""

    def __init__(self, getter: Callable = requests.get,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, minimum=1.0):
        if not (0.5 <= minimum <= 10.0):
            raise VenueProbeError("GET pacing must be between 0.5 and 10 seconds")
        self.getter, self.sleep, self.clock, self.minimum = getter, sleep, clock, minimum
        self.last: float | None = None
        self.calls = 0

    def __call__(self, url: str, **kwargs):
        current = self.clock()
        if self.last is not None:
            remaining = self.minimum - (current - self.last)
            if remaining > 0:
                self.sleep(remaining)
                current = self.clock()
        # Before request, so a failed HTTP still consumes the rate-limit slot.
        self.last = current
        self.calls += 1
        return self.getter(url, **kwargs)


def _safe_failure(exc: Exception) -> dict:
    # Never print exception strings: raw third-party/network text may contain
    # headers, request arguments or account identifying material.
    value = str(exc) if isinstance(exc, VenueProbeError) else ""
    match = re.search(r"msg_cd=([A-Za-z0-9_-]{1,32})\)", value)
    return {
        "type": type(exc).__name__ if type(exc).__name__ in
                ("VenueProbeError", "DataError", "KISMinuteError", "OSError",
                 "RequestException") else "OtherError",
        "provider_code": match.group(1) if match else None,
        "credential_values_disclosed": False,
    }


def _save_progress(root: Path, report: dict):
    tmp = root / ".batch_progress.tmp"
    dest = root / "batch_progress.json"
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    tmp.replace(dest)


def run(*, tasks: list[dict], root: str | Path, execute: bool = False,
        key: str = "", secret: str = "", token: str = "",
        getter: Callable = requests.get, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        pacing_seconds: float = 1.0, between_tasks_seconds: float = 2.0,
        max_failures: int = 3) -> dict:
    if len(tasks) > MAX_TASKS or not tasks:
        raise VenueProbeError("Empty or oversized research capture plan")
    if not (0 <= between_tasks_seconds <= 30.0):
        raise VenueProbeError("Task pause must be in 0..30 seconds")
    if not (1 <= max_failures <= 3):
        raise VenueProbeError("Fail-fast threshold must be 1..3")
    root = Path(root).expanduser().resolve()
    existing_status: list[dict] = []
    for task in tasks:
        folder = root / f"KIS_{task['market']}_EXT_{task['symbol']}_{task['session']}"
        if folder.exists():
            # Fail closed if an existing source was altered; do not overwrite it.
            _read_one(root, task["symbol"], task["session"], task["market"])
            status = "EXISTING_VERIFIED"
        else:
            status = "NOT_COLLECTED"
        existing_status.append({**task, "status": status})
    report = {
        "status": "DRY_RUN" if not execute else "IN_PROGRESS",
        "tasks": existing_status,
        "request_pacing_seconds": pacing_seconds,
        "between_tasks_seconds": between_tasks_seconds,
        "session_holiday_calendar_verified": False,
        "no_data_is_not_zero_trading": True,
        "authorization_required_for_execution": True,
        "secret_values_written": False,
        "usable_for_backtesting": False,
        "orders_enabled": False,
    }
    if not execute:
        return report
    if not (key and secret and token):
        raise VenueProbeError("Local KIS authentication required before execute")
    root.mkdir(parents=True, exist_ok=True)
    pacing = PacedGetter(getter=getter, sleep=sleep, clock=clock,
                         minimum=pacing_seconds)
    failures = 0
    for item in report["tasks"]:
        if item["status"] == "EXISTING_VERIFIED":
            continue
        try:
            if pacing.calls:
                sleep(between_tasks_seconds)
            got = capture(
                symbol=item["symbol"], session=item["session"], market=item["market"],
                out_dir=root, key=key, secret=secret, token=token,
                getter=pacing, pause=lambda _n: None,
            )
            # Source was written already; verify hashes before marking success.
            _read_one(root, item["symbol"], item["session"], item["market"])
            item["status"] = "COLLECTED_VERIFIED"
            item["minute_labels"] = got["audit"]["unique_minute_labels"]
            item["daily_status"] = got["audit"]["daily_comparison"]["status"]
        except Exception as exc:
            # Intentional isolation: one failing market must not erase
            # successfully collected market/day snapshots.
            item["status"] = "FAILED_CLOSED"
            item["error"] = _safe_failure(exc)
            failures += 1
            if failures >= max_failures or item["error"]["provider_code"] in {
                "EGW00201", "EGW00202", "EGW00133"
            }:
                report["status"] = "STOPPED_ON_FAILURE_LIMIT"
                _save_progress(root, report)
                break
        _save_progress(root, report)
    else:
        report["status"] = (
            "COMPLETED_WITH_FAILURES" if failures else "CAPTURES_COMPLETED"
        )
        _save_progress(root, report)
    report["http_get_requests"] = pacing.calls
    report["completed_snapshot_count"] = sum(
        x["status"] in ("COLLECTED_VERIFIED", "EXISTING_VERIFIED")
        for x in report["tasks"]
    )
    _save_progress(root, report)
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Bounded stock historical venue cohort, DRY RUN by default"
    )
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--sessions", default=DEFAULT_DATES)
    p.add_argument("--markets", default=DEFAULT_MARKETS)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--pacing-seconds", type=float, default=1.0)
    args = p.parse_args(argv)
    try:
        tasks = plan(symbols=args.symbols, sessions=args.sessions,
                     markets=args.markets)
        if args.execute:
            key, secret = os.getenv("KIS_APP_KEY", ""), os.getenv("KIS_APP_SECRET", "")
            if not key or not secret:
                raise VenueProbeError("KIS credentials not configured in local WSL")
            token = _kis_token()
        else:
            key = secret = token = ""
        result = run(tasks=tasks, root=args.out_dir,
                     execute=args.execute, key=key, secret=secret, token=token,
                     pacing_seconds=args.pacing_seconds)
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] not in (
            "STOPPED_ON_FAILURE_LIMIT", "COMPLETED_WITH_FAILURES") else 2
    except Exception as exc:
        err = _safe_failure(exc)
        p.exit(2, "FAIL CLOSED: " + json.dumps(err, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())

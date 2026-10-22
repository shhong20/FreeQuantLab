"""Read-only operational provenance and missing-slot incident evidence.

No service restarts, no writes, no ledger access, and no live order endpoints.
A gap remains an actual loss of contemporaneous public market observations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Callable

CADENCE = 600
SLOTS_PER_DAY = 144
L2_GRACE_SECONDS = 120
WS_OFFSET_AND_GRACE_SECONDS = 210
ROOT = Path("/home/hong/apps")
PUBLIC_DB_DIR = ROOT / "FreeQuantLab/state/microstructure"
RUNTIMES = {
    "core": ROOT / "FreeQuantLab",
    "observer": ROOT / "FreeQuantLab-observer-runtime",
    "clean": ROOT / "FreeQuantLab-clean-runtime",
}
SERVICES = {
    "freequant-daily-receipt.service": "core",
    "freequant-isolated-watchdog.service": "core",
    "freequant-market-archive.service": "core",
    "freequant-watchdog.service": "core",
    "freequant-shadow.service": "core",
    "freequant-exaone.service": "core",
    "freequant-qlib-lab.service": "core",
    "freequant-dashboard.service": "core",
    "freequant-clean-shadow.service": "clean",
    "freequant-clean-watchdog.service": "clean",
    "freequant-stock-history.service": "observer",
    "freequant-stock-observer.service": "observer",
    "freequant-microstructure.service": "observer",
    "freequant-microstructure-audit.service": "observer",
    "freequant-websocket.service": "observer",
    "freequant-websocket-audit.service": "observer",
    "freequant-websocket-backup.service": "observer",
}


def _readonly(db: str | Path) -> sqlite3.Connection:
    p = Path(db).resolve(strict=True)
    if not p.is_file():
        raise ValueError("Expected existing observation DB file")
    con = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA query_only=ON")
    return con


def _ranges(missing: list[int]) -> list[dict]:
    if not missing:
        return []
    results = []
    first = end = missing[0]
    for slot in missing[1:] + [None]:
        if slot is not None and slot == end + 1:
            end = slot
            continue
        results.append({
            "first_missing_utc": datetime.fromtimestamp(first * CADENCE, timezone.utc).isoformat(),
            "last_missing_utc": datetime.fromtimestamp(end * CADENCE, timezone.utc).isoformat(),
            "missing_slots": end - first + 1,
        })
        if slot is not None:
            first = end = slot
    return results


def audit_slot_window(db: str | Path, kind: str, *,
                      now: datetime | None = None) -> dict:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("UTC-aware now required")
    if kind not in ("l2", "websocket"):
        raise ValueError("Unsupported evidence kind")
    now_epoch = instant.timestamp()
    adjust = L2_GRACE_SECONDS if kind == "l2" else WS_OFFSET_AND_GRACE_SECONDS
    due = int((now_epoch - adjust) // CADENCE)
    first = due - SLOTS_PER_DAY + 1
    with _readonly(db) as con:
        if kind == "l2":
            rows = con.execute(
                "SELECT slot FROM observation WHERE slot BETWEEN ? AND ?",
                (first, due),
            ).fetchall()
            present = {int(row[0]) for row in rows}
        else:
            # Only completed capture windows count, never stale/failed sessions.
            rows = con.execute(
                """SELECT started_ms FROM sessions
                WHERE started_ms BETWEEN ? AND ?
                  AND status = 'BOUNDED_CAPTURE_COMPLETE'""",
                (first * CADENCE * 1000, (due + 1) * CADENCE * 1000 - 1),
            ).fetchall()
            present = {int(ms // (CADENCE * 1000)) for (ms,) in rows}
    missed = [slot for slot in range(first, due + 1) if slot not in present]
    intervals = _ranges(missed)
    longest = max((r["missing_slots"] for r in intervals), default=0)
    # Only if no more misses happen: a historical gap has completely aged
    # outside the next day's 144-slot rolling window by this moment.
    clear_at = (datetime.fromtimestamp(
        (missed[-1] + SLOTS_PER_DAY) * CADENCE + adjust, timezone.utc
    ).isoformat() if missed else None)
    return {
        "kind": kind,
        "window_start_utc": datetime.fromtimestamp(first * CADENCE, timezone.utc).isoformat(),
        "window_last_due_utc": datetime.fromtimestamp(due * CADENCE, timezone.utc).isoformat(),
        "expected_slots": SLOTS_PER_DAY,
        "captured_due_slots": SLOTS_PER_DAY - len(missed),
        "missing_due_slots": len(missed),
        "coverage_fraction": (SLOTS_PER_DAY - len(missed)) / SLOTS_PER_DAY,
        "longest_missing_run_slots": longest,
        "missing_runs": intervals,
        "historical_gap_exits_rolling_window_not_before_utc": clear_at,
        "future_capture_success_required": True,
        "not_recovered_or_backfilled": bool(missed),
        "quality_gate_approved": False,  # full independent SHA/quality check is separate
        "real_fills_verified": False,
        "real_orders_enabled": False,
        "automatic_model_promotion": False,
    }


def _run(args: list[str], runner: Callable = subprocess.run) -> str:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   f"unix:path=/run/user/{os.getuid()}/bus")
    done = runner(args, capture_output=True, text=True, timeout=10,
                  check=False, env=env)
    if done.returncode:
        raise RuntimeError("Read-only process metadata unavailable")
    return done.stdout.strip()


def runtime_matrix(*, roots: dict[str, Path] | None = None,
                   services: dict[str, str] | None = None,
                   runner: Callable = subprocess.run) -> dict:
    roots = RUNTIMES if roots is None else roots
    services = SERVICES if services is None else services
    revisions = {
        name: _run(["git", "-C", str(root), "rev-parse", "HEAD"], runner)
        for name, root in roots.items()
    }
    entries = []
    for unit, lane in services.items():
        if lane not in roots:
            raise ValueError("Unknown runtime for service")
        text = _run(["systemctl", "--user", "show", unit,
                     "-p", "WorkingDirectory", "-p", "Result",
                     "-p", "ExecMainStatus"], runner)
        fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        deployed = str(roots[lane])
        actual = fields.get("WorkingDirectory")
        entries.append({
            "service": unit, "runtime": lane,
            "git_sha": revisions[lane],
            "working_directory_matches_inventory": actual == deployed,
            "service_result": fields.get("Result"),
            "service_exit_code": fields.get("ExecMainStatus"),
        })
    return {
        "runtime_revisions": revisions,
        "versions_are_mixed": len(set(revisions.values())) > 1,
        "services": entries,
        "service_directory_drift": sorted(
            item["service"] for item in entries
            if not item["working_directory_matches_inventory"]),
        "any_service_nonzero": any(item["service_exit_code"] not in ("0", None)
                                   for item in entries),
        "deploy_automatically_approved": False,
    }


def inspect(*, l2: str | Path, websocket: str | Path,
            now: datetime | None = None, include_runtime: bool = True) -> dict:
    stamp = now or datetime.now(timezone.utc)
    report = {
        "captured_at_utc": stamp.astimezone(timezone.utc).isoformat(),
        "market_gaps": {
            "l2": audit_slot_window(l2, "l2", now=stamp),
            "websocket": audit_slot_window(websocket, "websocket", now=stamp),
        },
        "only_read_only_inspection": True,
        "existing_quality_gates_unchanged": True,
        "no_market_event_backfill": True,
    }
    if include_runtime:
        report["service_provenance"] = runtime_matrix()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Upbit observation gaps and service SHA inventory")
    ap.add_argument("--l2", default=str(PUBLIC_DB_DIR / "observations.sqlite3"))
    ap.add_argument("--websocket", default=str(PUBLIC_DB_DIR / "websocket.sqlite3"))
    ap.add_argument("--without-runtime", action="store_true")
    args = ap.parse_args(argv)
    try:
        data = inspect(l2=args.l2, websocket=args.websocket,
                       include_runtime=not args.without_runtime)
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "OPS_INSPECTION_FAILED_CLOSED",
                          "reason": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

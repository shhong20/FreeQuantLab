"""Independent market capture gap transitions; no source DB modifications."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from test_ops_integrity import _dbs
from quantlab.ops_integrity import CADENCE
from quantlab.market_gap_alerts import inspect_gap_alerts, run_gap_monitor, main

UTC = timezone.utc


def _stamp():
    return datetime(2026, 10, 22, 10, 35, tzinfo=UTC)


def _complete_both(l2: Path, ws: Path, stamp: datetime):
    l2_last = int((stamp.timestamp() - 120) // CADENCE)
    ws_last = int((stamp.timestamp() - 210) // CADENCE)
    with sqlite3.connect(l2) as db:
        db.execute("DELETE FROM observation")
        db.executemany("INSERT INTO observation(slot) VALUES (?)",
                       [(s,) for s in range(l2_last-143, l2_last+1)])
    with sqlite3.connect(ws) as db:
        db.execute("DELETE FROM sessions")
        db.executemany("INSERT INTO sessions(started_ms,status) VALUES (?,?)",
                       [(s*CADENCE*1000, "BOUNDED_CAPTURE_COMPLETE")
                        for s in range(ws_last-143, ws_last+1)])


def _state(db: Path) -> tuple[int, int, int]:
    with sqlite3.connect(db) as con:
        return (
            con.execute("SELECT COUNT(*) FROM active_alerts").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM notification_events "
                        "WHERE event_kind='triggered'").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM notification_events "
                        "WHERE event_kind='resolved'").fetchone()[0],
        )


def test_missing_slots_persist_incidents_without_duplicates(tmp_path):
    stamp = _stamp()
    due = int((stamp.timestamp()-210)//CADENCE)
    l2, ws = _dbs(tmp_path, due-143)
    alerts = tmp_path / "alerts.db"
    report = tmp_path / "history.json"
    before = (l2.read_bytes(), ws.read_bytes())
    first = run_gap_monitor(l2, ws, alerts, report, now=stamp)
    second = run_gap_monitor(l2, ws, alerts, report,
                             now=stamp+timedelta(seconds=1))
    assert first["status"] == "CAPTURE_SLOTS_MISSING_NO_RETROACTIVE_REPAIR"
    assert first["active_gap_alerts"] == 2
    assert second["alert_transition_count"] == 0
    assert _state(alerts) == (2, 2, 0)
    assert (l2.read_bytes(), ws.read_bytes()) == before
    assert not first["independent_raw_sha_and_quality_gate_approved"]
    assert not first["submitted_to_windows_api"]
    assert json.loads(report.read_text())["read_only"]


def test_complete_new_window_only_resolves_slot_gap_not_quality(tmp_path):
    stamp = _stamp()
    due = int((stamp.timestamp()-210)//CADENCE)
    l2, ws = _dbs(tmp_path, due-143)
    alerts, report = tmp_path/"alerts.db", tmp_path/"history.json"
    run_gap_monitor(l2, ws, alerts, report, now=stamp)
    _complete_both(l2, ws, stamp)
    done = run_gap_monitor(l2, ws, alerts, report, now=stamp)
    assert done["status"] == "CAPTURE_SLOTS_PRESENT_QUALITY_NOT_YET_CERTIFIED"
    assert done["active_gap_alerts"] == 0
    assert done["last_24h_slots_complete"]
    assert not done["independent_raw_sha_and_quality_gate_approved"]
    assert _state(alerts) == (0, 2, 2)


def test_missing_source_never_resolves_prior_incident(tmp_path):
    stamp = _stamp()
    due = int((stamp.timestamp()-210)//CADENCE)
    l2, ws = _dbs(tmp_path, due-143)
    alerts, report = tmp_path/"alerts.db", tmp_path/"history.json"
    run_gap_monitor(l2, ws, alerts, report, now=stamp)
    ws.unlink()
    with pytest.raises(FileNotFoundError):
        run_gap_monitor(l2, ws, alerts, report, now=stamp)
    assert _state(alerts) == (2, 2, 0)


def test_minor_single_gap_still_warned(tmp_path):
    stamp = _stamp()
    due = int((stamp.timestamp()-210)//CADENCE)
    l2, ws = _dbs(tmp_path, due-143)
    _complete_both(l2, ws, stamp)
    last = int((stamp.timestamp()-120)//CADENCE)
    with sqlite3.connect(l2) as con:
        con.execute("DELETE FROM observation WHERE slot=?", (last-15,))
    result, alerts = inspect_gap_alerts(l2, ws, now=stamp)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["code"] == "MARKET_L2_ROLLING_SLOT_GAP"
    assert not result["independent_raw_sha_and_quality_gate_approved"]


def test_cli_missing_sources_fail_closed_without_writing(tmp_path, capsys):
    rc = main(["--l2", str(tmp_path/"not-found.db"),
               "--websocket", str(tmp_path/"also-missing.db"),
               "--alert-db", str(tmp_path/"alerts.db"),
               "--report", str(tmp_path/"history.json")])
    assert rc == 2
    assert "MARKET_GAP_MONITOR_UNAVAILABLE" in capsys.readouterr().err
    assert not (tmp_path/"alerts.db").exists()

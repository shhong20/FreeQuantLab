"""System overview never mutates market observations, virtual ledgers or timers."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from quantlab.ops_integrity import CADENCE, audit_slot_window, runtime_matrix, inspect


def _dbs(tmp_path, first_due):
    l2 = tmp_path / "l2.sqlite3"
    ws = tmp_path / "ws.sqlite3"
    with sqlite3.connect(l2) as db:
        db.execute("CREATE TABLE observation(slot INTEGER PRIMARY KEY)")
        db.executemany("INSERT INTO observation(slot) VALUES (?)",
                       [(x,) for x in range(first_due, first_due+144)
                        if x not in range(first_due+14, first_due+19)])
    with sqlite3.connect(ws) as db:
        db.execute("CREATE TABLE sessions(started_ms INTEGER, status TEXT)")
        db.executemany("INSERT INTO sessions VALUES (?,?)",
                       [(slot*CADENCE*1000, "BOUNDED_CAPTURE_COMPLETE")
                        for slot in range(first_due,first_due+144)
                        if slot not in range(first_due+14,first_due+19)])
    return l2, ws


def test_l2_gap_provenance_and_rollout_is_readonly(tmp_path):
    stamp = datetime(2026, 10, 22, 10, 32, tzinfo=timezone.utc)
    due = int((stamp.timestamp() - 120) // CADENCE)
    l2, ws = _dbs(tmp_path, due-143)
    before = l2.read_bytes()
    result = audit_slot_window(l2, "l2", now=stamp)
    assert result["captured_due_slots"] == 139
    assert result["missing_due_slots"] == 5
    assert result["longest_missing_run_slots"] == 5
    assert len(result["missing_runs"]) == 1
    assert not result["quality_gate_approved"]
    assert result["not_recovered_or_backfilled"]
    assert result["historical_gap_exits_rolling_window_not_before_utc"]
    assert l2.read_bytes() == before


def test_websocket_failed_capture_is_not_counted(tmp_path):
    stamp = datetime(2026, 10, 22, 10, 35, tzinfo=timezone.utc)
    due = int((stamp.timestamp() - 210) // CADENCE)
    l2, ws = _dbs(tmp_path, due-143)
    with sqlite3.connect(ws) as db:
        db.execute("INSERT INTO sessions VALUES (?,?)",
                   ((due-143+15)*CADENCE*1000, "PARTIAL_FAILED"))
    result = audit_slot_window(ws, "websocket", now=stamp)
    assert result["missing_due_slots"] == 5
    assert result["captured_due_slots"] == 139
    assert not result["real_orders_enabled"]


def test_runtime_matrix_detects_old_shas_and_working_directory_drift(tmp_path):
    roots = {"core":tmp_path/"old", "clean":tmp_path/"clean"}
    services = {"legacy.service":"core","clean.service":"clean"}
    shas = {"old":"a"*40,"clean":"b"*40}
    def fake_run(argv,**kwargs):
        if argv[0] == "git":
            return SimpleNamespace(returncode=0,
                                   stdout=shas[Path(argv[2]).name]+"\n")
        unit = argv[3]
        path = roots["core"] if unit=="legacy.service" else tmp_path/"wrong"
        return SimpleNamespace(returncode=0,stdout=(
            f"WorkingDirectory={path}\nResult=success\nExecMainStatus=0\n"))
    r = runtime_matrix(roots=roots,services=services,runner=fake_run)
    assert r["versions_are_mixed"]
    assert r["service_directory_drift"] == ["clean.service"]
    assert not r["deploy_automatically_approved"]
    assert r["services"][0]["git_sha"] == "a"*40


def test_inspection_works_without_systemd_or_git(tmp_path):
    stamp=datetime(2026,10,22,10,32,tzinfo=timezone.utc)
    due=int((stamp.timestamp()-120)//CADENCE)
    l2,ws=_dbs(tmp_path,due-143)
    r=inspect(l2=l2,websocket=ws,now=stamp,include_runtime=False)
    assert "service_provenance" not in r
    assert r["only_read_only_inspection"]
    assert r["existing_quality_gates_unchanged"]


def test_unsupported_kind_fails_closed(tmp_path):
    with pytest.raises(ValueError,match="Unsupported"):
        audit_slot_window(tmp_path/"no.sqlite3","private_order")


def test_invalid_existing_database_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        audit_slot_window(tmp_path/"missing.sqlite3","l2")

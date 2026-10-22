"""Safety regression for optional systemd bounded public WebSocket backup.

Synthetic same-filesystem mirror is *explicitly mocked*, and never attested
as a true independent mount. Real cross-mount validation is exercised in the
installed WSL smoke test, not forged here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import quantlab.websocket_backup_cycle as cycle
from quantlab.websocket_incremental_archive import add_segment,verify_chain
from quantlab.websocket_archive_mirror import mirror as actual_mirror
from test_websocket_archive import add_session
from test_websocket_incremental_archive import TODAY


def fixture(tmp_path,monkeypatch,initial=2):
    live=tmp_path/"live.sqlite3"
    for i in range(initial):
        add_session(live,number=i)
    source=tmp_path/"source"
    for _ in range(initial):
        add_segment(live_db=live,root=source,through_date=TODAY,max_sessions=1)
    dest=tmp_path/"mirror"
    dest.mkdir()
    reports=tmp_path/"receipts"
    original_preflight=cycle._preflight
    def simulated_preflight(live,source,dest,*,min_free_bytes,max_archive_bytes):
        # Only same-mount fixture is simulated; actual runtime must always
        # validate physical mount IDs and free space.
        assert source.is_dir() and dest.is_dir()
        return {"test_simulation":True,"source_segments":len(list(source.glob("seg-*")))}
    monkeypatch.setattr(cycle,"_preflight",simulated_preflight)
    def simulated_mirror(**kwargs):
        kwargs.pop("use_verified_source_checkpoint",None)
        result=actual_mirror(**kwargs,require_other_mount=False)
        assert result["status"]=="SIMULATED_MIRROR_RESTORED_NOT_INDEPENDENT"
        result["status"]="SEPARATE_MOUNT_MIRROR_RESTORED_AND_VERIFIED"
        return result
    monkeypatch.setattr(cycle,"mirror",simulated_mirror)
    return live,source,dest,reports


def test_dry_run_is_readonly_and_does_not_create_receipts(tmp_path,monkeypatch):
    live,source,dest,reports=fixture(tmp_path,monkeypatch)
    # No mirror exists yet, so even a read-only preflight must fail closed.
    with pytest.raises(ValueError,match="Mirror is absent"):
        cycle.run_backup_cycle(live_db=live,archive_root=source,mirror_root=dest,
                               report_root=reports)
    assert not reports.exists()


def test_bounded_catchup_and_no_duplicate_or_delete(tmp_path,monkeypatch):
    live,source,dest,reports=fixture(tmp_path,monkeypatch)
    # Verify an existing archive was mirrored before preflight would be OK.
    actual_mirror(source_root=source,dest_root=dest,require_other_mount=False)
    for i in range(2,7):
        add_session(live,number=i)
    first=cycle.run_backup_cycle(live_db=live,archive_root=source,
                                 mirror_root=dest,report_root=reports,
                                 max_batches=3,max_sessions=2,perform=True)
    assert first["status"]=="MIRROR_RECOVERED_NO_BACKLOG_AT_CHECK"
    assert first["new_sessions_sealed"]==5
    assert first["new_source_segments"]==3
    assert first["mirror_total_frames_restored"]==49
    assert first["gap_at_receipt"]["not_yet_mirrored_completed_sessions"]==0
    assert not first["automated_storage_deletion_enabled"]
    assert not first["offsite_disaster_recovery_proven"]
    receipt=Path(first["receipt_path"])
    assert receipt.exists() and json.loads(receipt.read_text())["new_sessions_sealed"]==5
    assert verify_chain(source)["frames"]==49
    assert verify_chain(dest)["frames"]==49
    second=cycle.run_backup_cycle(live_db=live,archive_root=source,
                                  mirror_root=dest,report_root=reports,
                                  max_batches=3,max_sessions=2,perform=True)
    assert second["new_sessions_sealed"]==0
    assert second["new_source_segments"]==0
    assert len(list(reports.glob("*.json")))==2
    assert len(list(source.glob("seg-*")))==5


def test_backlog_remaining_is_explicit_and_not_promoted_to_full_backup(tmp_path,monkeypatch):
    live,source,dest,reports=fixture(tmp_path,monkeypatch)
    actual_mirror(source_root=source,dest_root=dest,require_other_mount=False)
    for i in range(2,8):add_session(live,number=i)
    r=cycle.run_backup_cycle(live_db=live,archive_root=source,
                             mirror_root=dest,report_root=reports,
                             max_batches=1,max_sessions=2,perform=True)
    assert r["status"]=="MIRROR_RECOVERED_WITH_NEW_SESSIONS_AFTER_SNAPSHOT"
    assert r["new_sessions_sealed"]==2
    assert r["gap_at_receipt"]["not_yet_mirrored_completed_sessions"]==4
    assert verify_chain(dest)["sessions"]==4


def test_mirror_recovery_failure_never_reports_success(tmp_path,monkeypatch):
    live,source,dest,reports=fixture(tmp_path,monkeypatch)
    actual_mirror(source_root=source,dest_root=dest,require_other_mount=False)
    add_session(live,number=2)
    def corrupted(**kwargs):
        raise ValueError("injected receiver raw SHA failure")
    monkeypatch.setattr(cycle,"mirror",corrupted)
    with pytest.raises(ValueError,match="injected receiver"):
        cycle.run_backup_cycle(live_db=live,archive_root=source,
                               mirror_root=dest,report_root=reports,
                               perform=True)
    assert len(list(reports.glob("*.json")))==0
    assert verify_chain(source)["sessions"]==3
    assert verify_chain(dest)["sessions"]==2


def test_guard_same_mount_must_reject_in_real_preflight(tmp_path):
    live=tmp_path/"live"
    live.write_bytes(b"sqlite fixture placeholder")
    src=tmp_path/"src";dst=tmp_path/"dst"
    src.mkdir();dst.mkdir()
    with pytest.raises(ValueError,match="another mounted volume"):
        cycle._preflight(live,src,dst,min_free_bytes=0,
                         max_archive_bytes=cycle.MAX_ARCHIVE_BYTES)


def test_missing_volume_and_invalid_limits_never_publish(tmp_path):
    with pytest.raises(ValueError,match="Invalid bounded"):
        cycle.run_backup_cycle(max_batches=0)
    with pytest.raises(ValueError,match="Invalid bounded"):
        cycle.run_backup_cycle(max_sessions=99)
    with pytest.raises(FileNotFoundError):
        cycle.run_backup_cycle(live_db=tmp_path/"missing",
                               archive_root=tmp_path/"none",
                               mirror_root=tmp_path/"none",perform=True)


def test_service_isolated_no_sudo_and_reboot_catchup():
    root=Path(__file__).resolve().parents[1]/"deploy"/"systemd"
    service=(root/"freequant-websocket-backup.service").read_text()
    timer=(root/"freequant-websocket-backup.timer").read_text()
    assert "quantlab.websocket_backup_cycle --run" in service
    assert "WorkingDirectory=/home/hong/apps/FreeQuantLab-observer-runtime" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=/home/hong/apps/FreeQuantLab/state/microstructure /mnt/d/FreeQuantLab-Backups" in service
    assert "TimeoutStartSec=300" in service
    assert "OnCalendar=*-*-* *:45:00" in timer
    assert "OnBootSec=15min" in timer and "Persistent=true" in timer
    assert "freequant-shadow" not in service
    assert "sudo " not in service


def test_new_completed_websocket_session_during_verification_is_bounded_warning(tmp_path,monkeypatch):
    live,source,dest,reports=fixture(tmp_path,monkeypatch)
    actual_mirror(source_root=source,dest_root=dest,require_other_mount=False)
    original_mirror=cycle.mirror
    original_gap=cycle.measure_gap
    def concurrent_finish(**kwargs):
        result=original_mirror(**kwargs)
        # One NEW completed session arrived while the verified restore ran.
        add_session(live,number=2)
        return result
    def simulated_realtime_gap(**kwargs):
        measured=original_gap(**kwargs)  # real source/mirror watermark checks
        # Synthetic 2026-10-21 fixture timestamps, NOT real-time WSL clock.
        measured["mirror_recovery_point_age_seconds"]=121
        measured["mirror_lag_versus_latest_completed_seconds"]=60
        return measured
    monkeypatch.setattr(cycle,"mirror",concurrent_finish)
    monkeypatch.setattr(cycle,"measure_gap",simulated_realtime_gap)
    result=cycle.run_backup_cycle(live_db=live,archive_root=source,
                                  mirror_root=dest,report_root=reports,perform=True)
    assert result["status"]=="MIRROR_RECOVERED_WITH_NEW_SESSIONS_AFTER_SNAPSHOT"
    assert result["completed_sessions_at_cycle_start"]==2
    assert result["preexisting_finalized_sessions_all_mirrored"]
    assert result["fresh_capture_lag_only"] is True
    assert result["unbacked_finalized_sessions_at_receipt"]==1
    assert verify_chain(dest)["sessions"]==2
    assert json.loads(Path(result["receipt_path"]).read_text())["unbacked_finalized_sessions_at_receipt"]==1


def test_old_backlog_does_not_pass_fresh_snapshot_rule(tmp_path,monkeypatch):
    live,source,dest,reports=fixture(tmp_path,monkeypatch)
    actual_mirror(source_root=source,dest_root=dest,require_other_mount=False)
    for i in range(2,8):
        add_session(live,number=i)
    result=cycle.run_backup_cycle(live_db=live,archive_root=source,
                                  mirror_root=dest,report_root=reports,
                                  max_batches=1,max_sessions=2,perform=True)
    assert result["status"]=="MIRROR_RECOVERED_WITH_NEW_SESSIONS_AFTER_SNAPSHOT"
    assert result["preexisting_finalized_sessions_all_mirrored"] is False
    assert result["fresh_capture_lag_only"] is False
    assert result["unbacked_finalized_sessions_at_receipt"]==4


def test_cli_exit_zero_only_when_new_small_fresh_and_cryptographically_restored(monkeypatch,capsys):
    def base():
        return {"status":"MIRROR_RECOVERED_WITH_NEW_SESSIONS_AFTER_SNAPSHOT",
                "fresh_capture_lag_only":True,"unbacked_finalized_sessions_at_receipt":1}
    monkeypatch.setattr(cycle,"run_backup_cycle",lambda **kwargs:base())
    assert cycle.main(["--run"])==0
    assert json.loads(capsys.readouterr().out)["unbacked_finalized_sessions_at_receipt"]==1
    monkeypatch.setattr(cycle,"run_backup_cycle",lambda **kwargs:{**base(),"fresh_capture_lag_only":False})
    assert cycle.main(["--run"])==2
    monkeypatch.setattr(cycle,"run_backup_cycle",lambda **kwargs:{
        **base(),"status":"MIRROR_RECOVERED_NO_BACKLOG_AT_CHECK",
        "unbacked_finalized_sessions_at_receipt":0})
    assert cycle.main(["--run"])==0

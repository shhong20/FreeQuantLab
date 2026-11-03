"""Windows scheduled 08:40/09:05 warm task receipts and daily paper evidence.

All Windows Scheduler results are synthetic here, not claims that a future
Windows scheduled action already occurred on the real machine.
"""
from __future__ import annotations

from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab import clean_windows_warm_chain as m
from test_clean_cohort import cohort,record
from test_clean_postopen_chain import _all_three

UTC=timezone.utc
NOW=datetime(2026,10,23,1,56,tzinfo=UTC)


def _args(cohort,tmp_path):
    db,protocol=cohort
    return {"db":db,"protocol":protocol,
            "preopen_dir":tmp_path/"preopen",
            "windows_warm_dir":tmp_path/"windows"}


def _write_receipt(directory:Path,slot:str,*,last_result=0,claimed=True,
                   config=True,wrong_time=False):
    # Windows 08:50 -> 2026-10-22 23:50Z; 09:10 -> 2026-10-23 00:10Z.
    assert slot in ("08:40","09:05")
    audit=datetime(2026,10,22,23,50,tzinfo=UTC) if slot=="08:40" else datetime(2026,10,23,0,10,tzinfo=UTC)
    warm=datetime(2026,10,22,23,40,tzinfo=UTC) if slot=="08:40" else datetime(2026,10,23,0,5,tzinfo=UTC)
    if wrong_time:warm=datetime(2026,10,22,20,4,tzinfo=UTC)
    obj={
        "schema":m.WINDOWS_SCHEMA,"task_name":m.WINDOWS_TASK,
        "audit_time_utc":audit.isoformat(),"kst_date":"2026-10-23",
        "scheduled_warm_slot_kst":slot,"evidence_window_valid":True,
        "existing_warm_config_match":config,
        "warm_last_run_time_utc":warm.isoformat(),
        "warm_last_task_result":last_result,
        "warm_executed_in_allowed_slot":not wrong_time,
        "successful_windows_warm_task_execution":claimed,
        "actual_wsl_remained_online_after_execution_proven":False,
        "actual_cold_boot_or_sleep_resume_proven":False,
        "created_historical_paper_orders":0,"broker_orders":0,
        "wrote_stock_or_crypto_prices":False,
        "mode":"Capture",
    }
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/f"windowswarm-20261023-{slot.replace(':','')}.json"
    data=(json.dumps(obj,ensure_ascii=False,sort_keys=True)+"\n").encode()
    path.write_bytes(data)
    path.with_suffix(".json.sha256").write_text(sha256(data).hexdigest()+"\n")
    return path


def test_no_windows_task_history_is_not_false_proof(cohort,tmp_path):
    record(*cohort)
    report=m.assemble(**_args(cohort,tmp_path),now=NOW)
    assert report["today_all_six_valid"] is True
    assert report["today_valid"]==6
    assert report["windows_warm_task_receipts"]["status"]=="WINDOWS_WARM_RECEIPTS_ABSENT"
    assert report["windows_warm_task_receipts"]["slots"]=={"08:40":False,"09:05":False}
    assert report["status"]=="DAILY_OBSERVATION_WITH_PARTIAL_BOOT_EVIDENCE"
    assert report["cumulative_missing"]==0
    assert report["broker_orders"]==0
    assert report["strategy_pnl"] is None


def test_both_actual_scheduled_windows_results_plus_wsl_receipts(cohort,tmp_path):
    record(*cohort)
    d=_args(cohort,tmp_path)
    _all_three(d["preopen_dir"])
    _write_receipt(d["windows_warm_dir"],"08:40")
    _write_receipt(d["windows_warm_dir"],"09:05")
    report=m.assemble(**d,now=NOW)
    assert report["status"]=="WINDOWS_WARM_AND_DAILY_OBSERVATION_EVIDENCE"
    assert report["windows_warm_task_receipts"]["status"]=="BOTH_WINDOWS_WARM_TASK_EXECUTIONS_ATTESTED"
    assert report["windows_warm_task_receipts"]["slots"]=={"08:40":True,"09:05":True}
    assert report["windows_warm_task_receipts"]["receipt_count"]==2
    assert report["wsl_readiness_3_windows_verified"] is True
    assert report["broker_orders"]==0
    assert report["trading_approved"] is False
    assert not report["pc_powered_on_while_windows_off_proven"]
    assert not report["windows_warm_task_receipts"]["cold_boot_or_host_awake_all_day_proven"]
    report_path=m.save(report,tmp_path/"result")
    assert m.verify(file=report_path,**d)==report
    assert report_path.stat().st_mode&0o777==0o600
    assert report_path.parent.stat().st_mode&0o777==0o700
    with pytest.raises(FileExistsError):
        m.save(report,tmp_path/"result")


def test_one_task_result_only_means_partial_proof(cohort,tmp_path):
    record(*cohort)
    d=_args(cohort,tmp_path)
    _write_receipt(d["windows_warm_dir"],"08:40")
    result=m.assemble(**d,now=NOW)
    assert result["windows_warm_task_receipts"]["status"]=="SINGLE_WINDOWS_WARM_TASK_EXECUTION_ATTESTED"
    assert result["windows_warm_task_receipts"]["slots"]=={"08:40":True,"09:05":False}
    assert result["status"]=="DAILY_OBSERVATION_WITH_PARTIAL_BOOT_EVIDENCE"


def test_nonzero_windows_warm_action_cannot_pass(cohort,tmp_path):
    record(*cohort)
    d=_args(cohort,tmp_path)
    _write_receipt(d["windows_warm_dir"],"08:40",last_result=1,claimed=False)
    result=m.assemble(**d,now=NOW)
    assert result["windows_warm_task_receipts"]["slots"]["08:40"] is False


def test_sleep_delayed_historical_task_result_does_not_count(cohort,tmp_path):
    record(*cohort)
    d=_args(cohort,tmp_path)
    _write_receipt(d["windows_warm_dir"],"08:40",wrong_time=True,claimed=False)
    result=m.assemble(**d,now=NOW)
    assert result["windows_warm_task_receipts"]["slots"]["08:40"] is False


def test_fake_success_and_rehashed_sha_fail_independent_timing(cohort,tmp_path):
    record(*cohort)
    d=_args(cohort,tmp_path)
    _write_receipt(d["windows_warm_dir"],"08:40",last_result=1,claimed=True)
    with pytest.raises(ValueError,match="false success"):
        m.assemble(**d,now=NOW)


def test_receipt_bit_flip_without_sha_fails(cohort,tmp_path):
    record(*cohort)
    d=_args(cohort,tmp_path)
    file=_write_receipt(d["windows_warm_dir"],"08:40")
    file.write_bytes(file.read_bytes()+b"garbage")
    with pytest.raises(ValueError,match="SHA mismatch"):
        m.assemble(**d,now=NOW)


def test_future_windows_task_does_not_backdate_prior_audit(cohort,tmp_path):
    record(*cohort)
    d=_args(cohort,tmp_path)
    original=m.assemble(**d,now=NOW)
    file=m.save(original,tmp_path/"result")
    _write_receipt(d["windows_warm_dir"],"08:40")
    # If original snapshot did not include an already existent receipt
    # created AFTER its capture, it cannot retroactively alter old evidence.
    # Here the synthetic Windows audit timestamp is BEFORE NOW, so verify
    # must detect new past-timestamp evidence instead of falsely certifying.
    with pytest.raises(ValueError,match="not independently reproducible"):
        m.verify(file=file,**d)


def test_missing_day_no_broker_orders_even_if_windows_tasks_work(cohort,tmp_path):
    d=_args(cohort,tmp_path)
    _write_receipt(d["windows_warm_dir"],"08:40")
    _write_receipt(d["windows_warm_dir"],"09:05")
    r=m.assemble(**d,now=NOW)
    assert not r["today_all_six_valid"]
    assert r["today_missing"]==6
    assert r["status"]=="TODAY_DUE_OBSERVATION_UNVERIFIED_NO_BACKFILL"
    assert r["broker_orders"]==0
    assert not r["trading_approved"]


def test_forged_trade_approval_rehash_rejected(cohort,tmp_path):
    record(*cohort)
    d=_args(cohort,tmp_path)
    report=m.assemble(**d,now=NOW)
    file=m.save(report,tmp_path/"result")
    body=m._payload({**report,"trading_approved":True})
    file.write_bytes(body)
    file.with_suffix(".json.sha256").write_text(sha256(body).hexdigest()+"\n")
    with pytest.raises(ValueError,match="not independently reproducible"):
        m.verify(file=file,**d)


def test_windows_task_audit_does_not_modify_warm_or_resume_task():
    root=Path(__file__).resolve().parents[1]/"ops/windows"
    cap=(root/"wsl_preopen_warm_capture.ps1").read_text()
    setup=(root/"wsl_preopen_receipt_task.ps1").read_text()
    assert 'Get-ScheduledTaskInfo -TaskName $taskName' in cap
    assert 'successful_windows_warm_task_execution' in cap
    assert 'actual_wsl_remained_online_after_execution_proven=$false' in cap
    assert 'Get-Date' not in cap or 'Start-ScheduledTask' not in cap
    assert 'ValidateSet("Audit","Capture")' in cap
    assert 'ValidateSet("Audit","Install")' in setup
    assert 'ExistingWarmLoginResumeTasksModified=$false' in setup
    assert 'LogonType Interactive' in setup
    assert 'RunLevel Limited' in setup
    assert 'StartWhenAvailable' in setup
    assert "AddHours(8).AddMinutes(50)" in setup
    assert "AddHours(9).AddMinutes(10)" in setup
    for bad in ("Restart-Computer","Stop-Computer","Remove-ScheduledTask",
                "Stop-ScheduledTask","Start-ScheduledTask","powercfg /set",
                "Invoke-WebRequest","--exec python","Set-ScheduledTask"):
        assert bad not in cap and bad not in setup


def test_existing_postopen_timer_runs_sidecar_without_another_wsl_timer():
    repo=Path(__file__).resolve().parents[1]
    service=(repo/"deploy/systemd/freequant-clean-postopen.service").read_text()
    assert service.count("ExecStart=")==2
    assert "quantlab.clean_postopen_chain" in service
    assert "quantlab.clean_windows_warm_chain" in service
    assert "--windows-warm-dir /mnt/c/Users/HONG/AppData/Local/FreeQuantLab/preopen_warm_audit" in service
    assert "ReadWritePaths=/home/hong/apps/FreeQuantLab/state/postopen_chain" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=read-only" in service
    assert "broker" not in service

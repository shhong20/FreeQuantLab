"""Preopen user-scheduled observations: no data writes or historical fills."""
from __future__ import annotations

from datetime import datetime,timezone
from hashlib import sha256
import json
import sqlite3
from pathlib import Path

import pytest

from quantlab import clean_preopen_readiness as p
from test_clean_cohort import cohort


UTC=timezone.utc


def _timer(unit):
    assert unit in p.TIMER_UNITS
    return {"unit":unit,"registered":True,"enabled":True,
            "active":True,"next_calendar_time":"Wed 2026-11-04 09:24:00 KST",
            "calendar_timer_ready":True}


def _public():
    return {"source":"Upbit public GET /v1/ticker (no auth)",
            "expected_markets":["KRW-BTC","KRW-ETH"],
            "public_ticker_reachable":True,
            "live_prices_not_persisted":True}


def _run(cohort,instant,**kwargs):
    db,manifest=cohort
    return p.inspect(db=db,protocol=manifest,now=instant,
                     timer_probe=kwargs.get("timer_probe",_timer),
                     ticker_probe=kwargs.get("ticker_probe",_public))


def test_preopen_ready_checks_six_frozen_accounts_with_no_market_price_payload(cohort):
    r=_run(cohort,datetime(2026,10,23,23,47,tzinfo=UTC))
    assert r["status"]=="READY_FOR_FUTURE_OBSERVATION_ONLY"
    assert r["kst_clock"]=="08:47"
    assert r["observation_window_status"]=="PREOPEN"
    assert r["frozen_accounts_verified"]==6
    assert r["calendar_timers_ready"] is True
    assert r["public_market_reachable"] is True
    assert r["future_09_24_observation_guaranteed"] is False
    assert r["windows_powered_on_guaranteed"] is False
    assert r["missed_days_backfilled"]==0
    assert r["past_virtual_orders_created"]==0
    assert r["live_orders"]==0
    assert r["strategy_returns"] is None
    assert "trade_price" not in json.dumps(r)
    with sqlite3.connect(cohort[0]) as c:
        assert c.execute("SELECT count(*) FROM shadow_events").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM shadow_quote_evidence").fetchone()[0]==0


def test_observation_window_is_only_readiness_not_decision_execution(cohort):
    r=_run(cohort,datetime(2026,10,24,0,7,tzinfo=UTC))
    assert r["status"]=="READY_FOR_FUTURE_OBSERVATION_ONLY"
    assert r["observation_window_status"]=="OBSERVATION_WINDOW"
    with sqlite3.connect(cohort[0]) as c:
        assert c.execute("SELECT count(*) FROM shadow_events").fetchone()[0]==0


def test_late_restart_is_logged_with_no_retroactive_paper_fill(cohort):
    r=_run(cohort,datetime(2026,10,24,2,11,tzinfo=UTC))
    assert r["status"]=="WINDOW_CLOSED_NO_HISTORICAL_REPLAY"
    assert r["observation_window_status"]=="AFTER_OBSERVATION_WINDOW"
    assert r["infrastructure_ready"] is True
    assert r["past_virtual_orders_created"]==0
    assert r["live_orders"]==0


def test_disabled_calendar_fails_closed_with_explicit_unit(cohort):
    def disabled(unit):
        result=_timer(unit)
        if unit=="freequant-clean-shadow.timer":
            result["active"]=False
            result["calendar_timer_ready"]=False
        return result
    r=_run(cohort,datetime(2026,10,24,0,4,tzinfo=UTC),timer_probe=disabled)
    assert r["status"]=="INFRA_READINESS_FAILED_NO_AUTOMATIC_ORDERS"
    assert r["calendar_timers_ready"] is False
    assert r["public_market_reachable"] is True
    assert r["broker_orders"] if "broker_orders" in r else r["live_orders"]==0


def test_no_public_connection_is_not_an_excuse_to_synthesize_a_quote(cohort):
    def timeout():
        raise TimeoutError("Public endpoint unreachable")
    r=_run(cohort,datetime(2026,10,24,0,4,tzinfo=UTC),
           ticker_probe=timeout)
    assert r["infrastructure_ready"] is False
    assert r["public_market_probe"]["public_ticker_reachable"] is False
    assert r["status"]=="INFRA_READINESS_FAILED_NO_AUTOMATIC_ORDERS"
    assert r["live_orders"]==0
    with sqlite3.connect(cohort[0]) as c:
        assert c.execute("SELECT count(*) FROM shadow_quote_evidence").fetchone()[0]==0


def test_frozen_account_identity_change_is_rejected(cohort):
    db,protocol=cohort
    with sqlite3.connect(db) as c:
        c.execute("UPDATE shadow_accounts SET label='changed' WHERE label='trend_ma20_80'")
    with pytest.raises(ValueError):
        p.inspect(db=db,protocol=protocol,
                  now=datetime(2026,10,24,0,4,tzinfo=UTC),
                  timer_probe=_timer,ticker_probe=_public)


def test_private_sha_receipt_does_not_overwrite_and_preserves_status(cohort,tmp_path):
    r=_run(cohort,datetime(2026,10,24,0,5,tzinfo=UTC))
    out=tmp_path/"readiness"
    path=p._save_receipt(r,out)
    assert path.exists()
    assert path.stat().st_mode & 0o777==0o600
    assert out.stat().st_mode & 0o777==0o700
    assert path.with_suffix(".json.sha256").stat().st_mode&0o777==0o600
    b=path.read_bytes()
    assert sha256(b).hexdigest()==path.with_suffix(".json.sha256").read_text().strip()
    assert json.loads(b)==r
    with pytest.raises(FileExistsError):
        p._save_receipt(r,out)
    assert path.read_bytes()==b


def test_windows_wsl_preopen_installer_does_not_modify_login_resume_or_power():
    script=(Path(__file__).resolve().parents[1]/
            "ops/windows/wsl_preopen_warm_task.ps1").read_text("utf-8")
    assert '[string]$Mode = "Audit"' in script
    assert 'ValidateSet("Audit","Install")' in script
    assert "FreeQuantLab-WSL-PreOpen-Warm" in script
    assert '--distribution Ubuntu --exec /usr/bin/true' in script
    assert "AddHours(8).AddMinutes(40)" in script
    assert "AddHours(9).AddMinutes(5)" in script
    assert 'LogonType Interactive' in script
    assert 'RunLevel Limited' in script
    assert "WakeToRun -eq $false" in script
    assert "Security.Principal.NTAccount" in script
    assert "Security.Principal.SecurityIdentifier" in script
    assert "$principalSidResolved -eq $true" in script
    assert '[string]$task.Principal.UserId -ieq $currentUser' not in script
    assert "ExistingWindowsTasksModified=$false" in script
    assert "Preopen task appeared concurrently; refusing to overwrite" in script
    for disallowed in (
        "Restart-Computer","Stop-Computer","wsl --shutdown",
        "WakeToRun $true","-WakeToRun","powercfg /change",
        "powercfg /set","Remove-ScheduledTask",
        "Stop-ScheduledTask","Start-ScheduledTask",
    ):
        assert disallowed not in script


def test_systemd_preopen_units_have_no_shadow_execution_or_backfill():
    deploy=Path(__file__).resolve().parents[1]/"deploy/systemd"
    service=(deploy/"freequant-clean-preopen.service").read_text()
    timer=(deploy/"freequant-clean-preopen.timer").read_text()
    assert "quantlab.clean_preopen_readiness" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=read-only" in service
    assert "ReadWritePaths=/home/hong/apps/FreeQuantLab/state/preopen_readiness" in service
    assert "OnCalendar=*-*-* 08:46:00 Asia/Seoul" in timer
    assert "OnCalendar=*-*-* 09:07:00 Asia/Seoul" in timer
    assert "OnCalendar=*-*-* 09:12:00 Asia/Seoul" in timer
    assert "OnStartupSec=3min" in timer
    assert "Persistent=true" in timer
    assert "shadow-cycle" not in service
    assert "run_cycle" not in service
    assert "broker" not in service

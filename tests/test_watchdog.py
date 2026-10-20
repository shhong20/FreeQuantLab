"""Read-only reports, persistent alert transitions, Windows API delivery stubs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from quantlab import shadow
from quantlab.data import demo_daily
from quantlab.settings import Settings
from quantlab.forward_reports import report, save_report, to_markdown
from quantlab.watchdog import (
    persist_alert_transitions, deliver_pending, check, _open_state,
)

ASSET = {"provider":"upbit","symbol":"KRW-BTC","kind":"crypto",
         "currency":"KRW","bars":320}
T20=datetime(2026,10,20,3,tzinfo=timezone.utc)
T21=datetime(2026,10,21,2,tzinfo=timezone.utc)
T22=datetime(2026,10,22,2,tzinfo=timezone.utc)


def candles(end):
    d=demo_daily(320)
    d["date"]=pd.date_range(end=end,periods=320)
    return d


@pytest.fixture()
def ledger(tmp_path):
    path=tmp_path/"shadow.db"
    shadow.bootstrap(path,[ASSET],Settings(),now=T20,
                     fetch_fn=lambda a:candles("2026-10-19"))
    return path


def _forward(db,now,date_end,price=100):
    return shadow.run_cycle(db,[ASSET],now=now,
       fetch_fn=lambda _:candles(date_end),
       quote_fn=lambda p,s,*,now:(price,now.date().isoformat()))


def test_initial_report_has_no_fabricated_returns(ledger,tmp_path):
    data=report(ledger,now=T20)
    assert data["real_orders_enabled"] is False
    assert data["strategy_auto_promotion_enabled"] is False
    assert len(data["strategies"])==3
    assert all(t["recorded_days"]==0 and t["observed_total_return"] is None
               and t["observed_max_drawdown"] is None for t in data["strategies"])
    assert "—" in to_markdown(data)
    a,b=save_report(data,tmp_path/"forward")
    assert json.loads(a.read_text())["report_date_kst"]=="2026-10-20"
    assert "첫" not in b.read_text() or "관찰" in b.read_text()
    assert a.stat().st_mode & 0o777 == 0o600
    assert b.stat().st_mode & 0o777 == 0o600


def test_true_first_forward_observation_produces_observed_metrics(ledger):
    _forward(ledger,T21,"2026-10-20",price=100.)
    data=report(ledger,now=T21)
    assert data["summary"]["recorded_events"]==3
    assert all(t["recorded_days"]==1 for t in data["strategies"])
    assert all(t["missing_intermediate_utc_days"]==0 for t in data["strategies"])
    assert all(t["first_observed_date"]=="2026-10-20" for t in data["strategies"])
    assert all(t["comparison_observations_aligned"] for t in data["strategies"])
    assert data["summary"]["virtual_orders"]>=1


def test_report_accounts_are_not_fused_into_fake_portfolio(ledger):
    _forward(ledger,T21,"2026-10-20")
    data=report(ledger,now=T21)
    assert "portfolio_return" not in data
    assert "annualized_sharpe" not in json.dumps(data)
    assert all("observed_total_return" in x for x in data["strategies"])


def test_report_does_not_mutate_shadow_database(ledger,tmp_path):
    before=Path(ledger).read_bytes()
    data=report(ledger,now=T20)
    save_report(data,tmp_path/"reports")
    after=Path(ledger).read_bytes()
    assert before==after


def test_alert_transitions_are_deduplicated_and_resolution_is_recorded(tmp_path):
    state=tmp_path/"alerts.db"
    issue={"severity":"warning","code":"DECISION_MISSING","message":"daily missing",
           "symbol":"KRW-BTC","strategy":"trend","expected_decision_date":"2026-10-20"}
    one=persist_alert_transitions(state,[issue],now=T21)
    two=persist_alert_transitions(state,[issue],now=T21+timedelta(minutes=25))
    assert len(one["transitions"])==1
    assert two["transitions"]==[]
    assert persist_alert_transitions(state,[],now=T22)["transitions"][0]["kind"]=="resolved"
    with _open_state(state) as con:
        assert con.execute("SELECT COUNT(*) FROM active_alerts").fetchone()[0]==0
        assert con.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0]==2


def test_new_missing_day_creates_new_alert_not_silent_carryover(tmp_path):
    state=tmp_path/"alert.db"
    issue={"severity":"warning","code":"DECISION_MISSING","message":"miss",
            "expected_decision_date":"2026-10-20"}
    persist_alert_transitions(state,[issue],now=T21)
    later={**issue,"expected_decision_date":"2026-10-21"}
    changed=persist_alert_transitions(state,[later],now=T22)
    assert {e["kind"] for e in changed["transitions"]}=={"triggered","resolved"}


def test_persistent_critical_generates_24h_reminder_only(tmp_path):
    state=tmp_path/"alert.db"
    issue={"severity":"critical","code":"RISK_HALTED","message":"virtual stop",
           "symbol":"KRW-BTC","strategy":"hold"}
    assert persist_alert_transitions(state,[issue],now=T20)["transitions"][0]["kind"]=="triggered"
    assert not persist_alert_transitions(state,[issue],now=T20+timedelta(hours=20))["transitions"]
    again=persist_alert_transitions(state,[issue],now=T20+timedelta(hours=25))
    assert again["transitions"][0]["kind"]=="critical_reminder"


def test_windows_delivery_failure_retries_without_recreating_event(tmp_path):
    state=tmp_path/"alert.db"
    issue={"severity":"critical","code":"RISK_HALTED","message":"virtual stop"}
    persist_alert_transitions(state,[issue],now=T20)
    def failed(title,msg):
        raise RuntimeError("No active Windows desktop")
    result=deliver_pending(state,toast=True,notify=failed,now=T20)
    assert result["delivery_failed"]==1
    assert deliver_pending(state,toast=True,notify=failed,now=T20)["delivery_failed"]==1
    seen=[]
    ok=deliver_pending(state,toast=True,
                       notify=lambda title,msg:seen.append((title,msg)),now=T20)
    assert ok["submitted_to_windows_api"]==1
    assert len(seen)==1 and "RISK_HALTED" in seen[0][1]
    assert deliver_pending(state,toast=True,notify=failed,now=T20)["delivery_failed"]==0
    with _open_state(state) as con:
        assert con.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0]==1


def test_no_windows_alert_delivery_without_events(ledger,tmp_path):
    state=tmp_path/"alerts.db"
    result=check(shadow_db=ledger,state_db=state,report_dir=tmp_path/"reports",
                 now=T20,windows_notifications=True,health_check=lambda:True)
    assert result["active_alerts"]==0
    assert result["windows_delivery"]["submitted_to_windows_api"]==0
    assert result["status"]=="OK"
    assert len(result["reports"])==2
    assert not check(shadow_db=ledger,state_db=state,report_dir=tmp_path/"reports",
                     now=T20+timedelta(minutes=5),
                     windows_notifications=False,health_check=lambda:True)["alert_transitions"]


def test_broken_shadow_database_alert_is_persistent_and_reports_not_fabricated(tmp_path):
    missing=tmp_path/"missing.db"
    state=tmp_path/"alerts.db"
    res=check(shadow_db=missing,state_db=state,report_dir=tmp_path/"reports",
              now=T20,windows_notifications=False,health_check=lambda:True)
    assert res["status"]=="DEGRADED" and not res["reports"]
    assert res["active_alerts"]==1
    assert res["alert_transitions"][0]["code"]=="SHADOW_READ_FAILED"
    assert list((tmp_path/"reports").glob("*"))==[]
    assert not missing.exists()


def test_dashboard_outage_is_independent_alert(ledger,tmp_path):
    r=check(shadow_db=ledger,state_db=tmp_path/"state.db",
            report_dir=tmp_path/"reports",now=T20,
            windows_notifications=False,health_check=lambda:False)
    assert r["active_alerts"]==1
    assert r["alert_transitions"][0]["code"]=="DASHBOARD_UNREACHABLE"


def test_delivery_without_windows_setting_keeps_inbox_and_no_toast(tmp_path):
    state=tmp_path/"alert.db"
    persist_alert_transitions(state,[{"severity":"warning","code":"X","message":"m"}],now=T20)
    result=deliver_pending(state,toast=False,notify=lambda *a:pytest.fail("unexpected toast"),now=T20)
    assert result["local_only"]==1
    with _open_state(state) as con:
        assert con.execute("SELECT delivery_status FROM notification_events").fetchone()[0]=="not_requested"


def test_stale_notifications_do_not_interrupt_after_machine_reboot(tmp_path):
    state=tmp_path/"alert.db"
    persist_alert_transitions(state,[{"severity":"critical","code":"X","message":"m"}],now=T20)
    result=deliver_pending(state,toast=True,
                           notify=lambda *a:pytest.fail("stale should skip"),
                           now=T22)
    assert result["local_only"]==1

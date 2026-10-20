"""Isolated paper authorization and independently durable due-day alerts.

Synthetic account rows and offline dependency substitutions are deliberately
not claims of Qlib, Exaone, live fills or real forward returns.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from quantlab.prospective import _canonical
from quantlab.isolated_scheduler import (
    read_schedule, run_scheduled, SchedulerError, eligible_decision,
)
from quantlab.isolated_watchdog import inspect_isolated, check_isolated


EARLY = datetime(2026,10,21,1,19,tzinfo=timezone.utc)
DUE = datetime(2026,10,21,1,20,tzinfo=timezone.utc)
T20 = datetime(2026,10,20,8,15,tzinfo=timezone.utc)


def _write_signed(target,body):
    signed=dict(body)
    signed["sha256"]=sha256(_canonical(signed)).hexdigest()
    target.write_bytes(_canonical(signed)+b"\n")
    return signed


@pytest.fixture()
def policy(tmp_path):
    for lane in ("agent_lab_shadow","qlib_lab_shadow"):
        db=tmp_path/(lane+".db")
        with sqlite3.connect(db) as conn:
            conn.executescript("""
                CREATE TABLE shadow_accounts(
                    track_id TEXT PRIMARY KEY,symbol TEXT NOT NULL,label TEXT NOT NULL);
                CREATE TABLE shadow_events(
                    track_id TEXT NOT NULL,decision_date TEXT NOT NULL,
                    session_date TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE shadow_runs(
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,created_at TEXT NOT NULL);
            """)
            for sym in ("KRW-BTC","KRW-ETH"):
                conn.execute("INSERT INTO shadow_accounts VALUES(?,?,?)",
                             (lane+sym,sym,"sealed_test"))
    path=tmp_path/"schedule.json"
    body={
      "schema_version":1,"mode":"virtual_observation_only",
      "activation_basis":"user_authorized_isolated_automation_2026-10-20",
      "registered_at_utc":T20.isoformat(),
      "first_decision_utc":"2026-10-20","last_decision_utc":"2027-01-18",
      "production_db":str(tmp_path/"shadow.db"),
      "agent_db":str(tmp_path/"agent_lab_shadow.db"),
      "exaone_db":str(tmp_path/"agent_lab_shadow.db"),
      "qlib_db":str(tmp_path/"qlib_lab_shadow.db"),
      "allow_live_broker_orders":False,"allow_model_retraining":False,
      "allow_auto_promotion":False,"allow_historical_backfill":False,
      "requires_original_frozen_strategies":True,
      "lanes":{
          "exaone":{"db":str(tmp_path/"agent_lab_shadow.db"),
                    "plan":str(tmp_path/"exaone.json"),"plan_sha256":"a"*64,
                    "expected_frozen_tracks":2,"auto_paper_only":True},
          "qlib":{"db":str(tmp_path/"qlib_lab_shadow.db"),
                  "plan":str(tmp_path/"qlib.json"),"plan_sha256":"b"*64,
                  "expected_frozen_tracks":2,"auto_paper_only":True}
      },
    }
    return path,_write_signed(path,body),tmp_path


def test_exact_manifest_hash_paths_permissions_and_authorized_utc_dates(policy):
    path,body,_=policy
    assert read_schedule(path)==body
    assert eligible_decision(body,T20) is None
    assert eligible_decision(body,EARLY)==date(2026,10,20)
    assert eligible_decision(body,datetime(2027,1,19,0,20,tzinfo=timezone.utc))==date(2027,1,18)
    assert eligible_decision(body,datetime(2027,1,20,0,20,tzinfo=timezone.utc)) is None
    altered=json.loads(path.read_text())
    altered["allow_live_broker_orders"]=True
    path.write_text(json.dumps(altered))
    with pytest.raises(SchedulerError,match="hash invalid"):
        read_schedule(path)


def test_even_newly_resigned_policy_cannot_allow_orders_or_retraining(policy):
    path,body,_=policy
    body={k:v for k,v in body.items() if k!="sha256"}
    body["allow_auto_promotion"]=True
    _write_signed(path,body)
    with pytest.raises(SchedulerError,match="unsupported permissions"):
        read_schedule(path)


def test_reject_wrong_db_lane_broker_or_shared_database(policy):
    path,body,tmp=policy
    body={k:v for k,v in body.items() if k!="sha256"}
    body["qlib_db"]=str(tmp/"shadow.db")
    _write_signed(path,body)
    with pytest.raises(SchedulerError,match="Ledger isolation"):
        read_schedule(path)


def test_scheduler_outside_window_never_invokes_virtual_trading(policy,monkeypatch):
    path,body,_=policy
    from quantlab import isolated_scheduler as sched
    monkeypatch.setattr(sched,"verify_lane",lambda p,n,*,now:{"sha256":body["lanes"][n]["plan_sha256"]})
    def forbid(*args,**kwargs):
        pytest.fail("Cannot execute virtual fill prior to authorized window")
    for lane in ("exaone","qlib"):
        result=run_scheduled(lane,policy_path=path,now=T20,
            agent_runner=forbid,qlib_runner=forbid)
        assert result["status"]=="OUTSIDE_AUTHORIZED_UTC_WINDOW"
        assert result["virtual_events"]==0


def test_two_scheduled_runners_bind_only_sealed_plan_and_ledger(policy,monkeypatch):
    path,body,_=policy
    from quantlab import isolated_scheduler as sched
    monkeypatch.setattr(sched,"verify_lane",lambda p,n,*,now:{"sha256":body["lanes"][n]["plan_sha256"]})
    calls=[]
    def fake_exaone(plan,db,*,now,production_db):
        calls.append(("exaone",plan,db,production_db))
        return {"status":"OK","real_orders_enabled":False,"results":[{"status":"WAIT_NEW_BAR"}]}
    def fake_qlib(plan,db,*,now,production_db,agent_db):
        calls.append(("qlib",plan,db,production_db,agent_db))
        return {"status":"OK","real_orders_enabled":False,"results":[{"status":"WAIT_NEW_BAR"}]}
    r=run_scheduled("exaone",policy_path=path,now=EARLY,
                    agent_runner=fake_exaone)
    s=run_scheduled("qlib",policy_path=path,now=EARLY,
                    qlib_runner=fake_qlib)
    assert r["decision_utc_day"]==s["decision_utc_day"]=="2026-10-20"
    assert len(calls)==2
    assert calls[0][2]==body["exaone_db"] and calls[1][2]==body["qlib_db"]
    assert not r["real_orders_enabled"] and not s["real_orders_enabled"]


def test_scheduled_runner_propagates_paper_error_and_never_claims_success(policy,monkeypatch):
    path,body,_=policy
    from quantlab import isolated_scheduler as sched
    monkeypatch.setattr(sched,"verify_lane",lambda p,n,*,now:{"sha256":"a"*64})
    def failed(*args,**kwargs):
        return {"status":"ERROR","real_orders_enabled":False}
    with pytest.raises(SchedulerError,match="failed closed"):
        run_scheduled("exaone",policy_path=path,now=EARLY,agent_runner=failed)


def test_independent_watchdog_does_not_raise_missing_before_due_grace(policy):
    path,body,_=policy
    good=lambda p,n,*,now:{"status":"PASS"}
    a=inspect_isolated(policy_path=path,now=EARLY,verify_func=good)
    assert a["status"]=="WAITING_FIRST_DUE"
    assert not a["alerts"]
    assert len(a["coverage"])==4
    assert all(row["due_utc_days"]==0 for row in a["coverage"])
    b=inspect_isolated(policy_path=path,now=DUE,verify_func=good)
    assert b["status"]=="WARNING"
    assert all(x["missing_due_days"]==1 for x in b["coverage"])
    assert sum(x["code"]=="ISOLATED_DUE_DAY_MISSING" for x in b["alerts"])==4


def test_independent_watchdog_deduplicates_and_resolves_alerts_without_backfill(policy):
    path,body,tmp=policy
    good=lambda p,n,*,now:{"status":"PASS"}
    state=tmp/"isolated-watchdog.db"
    reports=tmp/"independent-reports"
    canonical=tmp/"watchdog-history.json"
    canonical.write_text('{"original":"unmodified"}')
    first=check_isolated(policy_path=path,state_db=state,report_dir=reports,
                         now=DUE,verify_func=good)
    assert first["status"]=="WARNING" and first["active_alerts"]==4
    assert len(first["alert_transitions"])==4
    again=check_isolated(policy_path=path,state_db=state,report_dir=reports,
                         now=DUE+timedelta(minutes=6),verify_func=good)
    assert again["alert_transitions"]==[]
    assert canonical.read_text()=='{"original":"unmodified"}'
    for name in ("agent_lab_shadow","qlib_lab_shadow"):
        with sqlite3.connect(tmp/(name+".db")) as c:
            for track, in c.execute("SELECT track_id FROM shadow_accounts").fetchall():
                c.execute("INSERT INTO shadow_events VALUES (?,?,?,?)",(
                    track,"2026-10-20","2026-10-21",
                    "2026-10-21T00:18:00+00:00"))
    done=check_isolated(policy_path=path,state_db=state,report_dir=reports,
                        now=DUE+timedelta(minutes=9),verify_func=good)
    assert done["status"]=="COLLECTING"
    assert done["active_alerts"]==0
    assert len(done["alert_transitions"])==4
    assert (tmp/"isolated-watchdog-history.json").exists()
    assert not any("return" in str(x).lower() for x in done["alert_transitions"])


def test_integrity_failure_is_critical_and_independent_of_other_lane(policy):
    path,body,_=policy
    def verify(p,n,*,now):
        if n=="qlib":
            raise RuntimeError("Corrupt actual model")
    s=inspect_isolated(policy_path=path,now=DUE,verify_func=verify)
    assert s["status"]=="DEGRADED"
    assert any(a["code"]=="ISOLATED_LANE_INTEGRITY_FAILED" and
               a["strategy"]=="qlib" for a in s["alerts"])
    assert all(a["strategy"]=="exaone" for a in s["alerts"]
               if a["code"]=="ISOLATED_DUE_DAY_MISSING")
    assert s["no_observed_returns_exposed"]


def test_last_cycle_error_alerts_without_fabricating_new_trades(policy):
    path,body,tmp=policy
    with sqlite3.connect(tmp/"qlib_lab_shadow.db") as c:
        c.execute("INSERT INTO shadow_runs(status,created_at) VALUES('ERROR',?)",
                  (EARLY.isoformat(),))
    s=inspect_isolated(policy_path=path,now=EARLY,
                       verify_func=lambda p,n,*,now:None)
    assert s["status"]=="WARNING"
    assert any(a["code"]=="ISOLATED_LAST_CYCLE_ERROR" and a["strategy"]=="qlib"
               for a in s["alerts"])


def test_late_wsl_restart_never_creates_virtual_orders(policy, monkeypatch):
    """Persistent=true systemd catch-up at noon cannot execute 09:xx trades."""
    path, body, _ = policy
    from quantlab import isolated_scheduler as sched
    monkeypatch.setattr(sched, "verify_lane",
                        lambda p,n,*,now: {"sha256":body["lanes"][n]["plan_sha256"]})
    def refuse(*args,**kwargs):
        pytest.fail("A delayed persistent-timer activation must not fetch quote or trade")
    for moment in (
        datetime(2026,10,21,0,9,59,tzinfo=timezone.utc),
        datetime(2026,10,21,1,20,tzinfo=timezone.utc),
        datetime(2026,10,21,3,45,tzinfo=timezone.utc),
        datetime(2026,10,22,12,0,tzinfo=timezone.utc),
    ):
        assert eligible_decision(body,moment) is None
        for lane in ("exaone","qlib"):
            result=run_scheduled(lane,policy_path=path,now=moment,
                                 agent_runner=refuse,qlib_runner=refuse)
            assert result["status"]=="OUTSIDE_AUTHORIZED_UTC_WINDOW"
            assert result["virtual_events"]==0
    for hour,minute in ((0,10),(0,18),(0,31),(1,5),(1,19)):
        moment=datetime(2026,10,21,hour,minute,tzinfo=timezone.utc)
        assert eligible_decision(body,moment)==date(2026,10,20)


def test_broken_policy_triggers_persistent_critical_warning_and_recovers(policy):
    path,body,tmp=policy
    before=path.read_bytes()
    broken=json.loads(path.read_text())
    broken["allow_live_broker_orders"]=True
    path.write_text(json.dumps(broken))
    state=tmp/"isolated-watchdog.db"
    reports=tmp/"reports"
    result=check_isolated(policy_path=path,state_db=state,report_dir=reports,
                          now=EARLY-timedelta(minutes=18))
    assert result["status"]=="DEGRADED"
    assert result["active_alerts"]==1
    assert any(t["code"]=="ISOLATED_AUTOMATION_POLICY_INVALID"
               for t in result["alert_transitions"])
    report=json.loads((reports/"2026-10-21.json").read_text())
    assert report["status"]=="DEGRADED"
    assert report["automation_policy_sha256"] is None
    assert report["real_orders_enabled"] is False
    assert report["coverage"]==[]
    duplicate=check_isolated(policy_path=path,state_db=state,
                             report_dir=reports,now=EARLY-timedelta(minutes=17))
    assert duplicate["alert_transitions"]==[]
    path.write_bytes(before)
    good=check_isolated(policy_path=path,state_db=state,report_dir=reports,
                        now=EARLY-timedelta(minutes=16),
                        verify_func=lambda *args,**kwargs:None)
    assert good["status"]=="WAITING_FIRST_DUE"
    assert good["active_alerts"]==0
    assert any(t["code"]=="ISOLATED_AUTOMATION_POLICY_INVALID"
               for t in good["alert_transitions"])
    assert (tmp/"isolated-watchdog-history.json").exists()


def test_missing_authorization_still_persists_critical_alert(policy):
    path,_,tmp=policy
    path.unlink()
    result=check_isolated(policy_path=path,
                          state_db=tmp/"isolated-watchdog.db",
                          report_dir=tmp/"reports",now=EARLY)
    assert result["status"]=="DEGRADED"
    assert len(result["alert_transitions"])==1
    assert result["alert_transitions"][0]["code"]=="ISOLATED_AUTOMATION_POLICY_INVALID"

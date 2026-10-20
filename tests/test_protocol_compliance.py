"""Prospective protocol compliance, immutable daily runner guard and no-backfill."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
from urllib.request import urlopen

import pandas as pd
import pytest

from quantlab.dashboard import make_handler
from quantlab.data import demo_daily
from quantlab.protocol_compliance import assess_collection, verify_frozen_accounts
from quantlab.prospective import lock_protocol, ProtocolError
from quantlab.shadow import bootstrap, run_cycle
from quantlab.settings import Settings
from quantlab.watchdog import check

ASSET = {"provider":"upbit","symbol":"KRW-BTC","kind":"crypto","currency":"KRW","bars":320}
T20 = datetime(2026,10,20,5,tzinfo=timezone.utc)
BEFORE = datetime(2026,10,22,1,19,tzinfo=timezone.utc)
AFTER = datetime(2026,10,22,1,20,tzinfo=timezone.utc)


def bars(day="2026-10-21"):
    df=demo_daily(320)
    df["date"]=pd.date_range(end=day,periods=320)
    return df


@pytest.fixture()
def study(tmp_path):
    db=tmp_path/"shadow.db"
    protocol=tmp_path/"locked.json"
    bootstrap(db,[ASSET],Settings(),now=T20,
              fetch_fn=lambda asset:bars("2026-10-19"))
    lock_protocol(db,protocol,now=T20,days=30)
    return db,protocol,tmp_path


def test_first_due_only_after_daily_grace_and_never_results(study):
    db,manifest,_=study
    before=assess_collection(db,manifest,now=BEFORE)
    assert before["status"]=="REGISTERED_WAITING_FIRST_DUE"
    assert before["expected_due_days_per_account"]==0
    assert not before["issues"]
    assert before["includes_return_metrics"] is False
    late=assess_collection(db,manifest,now=AFTER)
    assert late["status"]=="BREACH_OR_MISSING"
    assert late["expected_due_days_per_account"]==1
    assert len(late["accounts"])==3
    assert all(t["missing_due_days"]==1 for t in late["accounts"])
    assert "observed_net_return" not in json.dumps(late)
    assert not late["eligible_for_promotion"]


def test_actual_one_new_utc_day_removes_due_missing(study):
    db,manifest,_=study
    # 2026-10-22 UTC: newly closed decision day is Oct 21.
    price=float(bars()["close"].iloc[-1])
    result=run_cycle(
        db,[ASSET],now=datetime(2026,10,22,1,18,tzinfo=timezone.utc),
        fetch_fn=lambda _:bars(),
        quote_fn=lambda p,s,*,now:(price,now.date().isoformat()),
        protocol_path=manifest,
    )
    assert result["status"]=="OK",result["errors"]
    assert sum(r["status"]=="RECORDED" for r in result["results"])==3
    after=assess_collection(db,manifest,now=AFTER)
    assert after["status"]=="COLLECTING_AS_PREDECLARED"
    assert all(t["observed_due_days"]==1 and t["missing_due_days"]==0 for t in after["accounts"])
    assert all(set(t)=={"symbol","strategy","due_days","observed_due_days",
                         "missing_due_days","first_missing_utc_day"} for t in after["accounts"])
    rerun=run_cycle(
        db,[ASSET],now=datetime(2026,10,22,1,30,tzinfo=timezone.utc),
        fetch_fn=lambda _:bars(),
        quote_fn=lambda p,s,*,now:pytest.fail("Do not need another ticker"),
        protocol_path=manifest,
    )
    assert rerun["status"]=="OK"
    assert sum(r["status"]=="RECORDED" for r in rerun["results"])==0
    with sqlite3.connect(db) as c:
        assert c.execute("select count(*) from shadow_events").fetchone()[0]==3


def test_account_identity_mutation_fails_before_market_requests(study):
    db,manifest,_=study
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shadow_accounts SET registered_utc=? WHERE label='trend_ma20_80'",
                    ("2026-10-20T03:00:00+00:00",))
    status=assess_collection(db,manifest,now=AFTER)
    assert status["status"]=="INVALID_PROTOCOL_IDENTITY"
    assert status["issues"][0]["code"]=="PROTOCOL_ACCOUNT_IDENTITY_FAILED"
    with pytest.raises(ProtocolError,match="fingerprint"):
        run_cycle(db,[ASSET],now=AFTER,fetch_fn=lambda _:pytest.fail("Fetch must not occur"),
                  quote_fn=lambda *args,**kwargs:pytest.fail("Ticker must not occur"),
                  protocol_path=manifest)
    with sqlite3.connect(db) as con:
        assert con.execute("select count(*) from shadow_events").fetchone()[0]==0


def test_clock_rollback_and_deleted_manifest_fail_closed(study):
    db,manifest,_=study
    with sqlite3.connect(db) as conn:
        accounts=conn.execute("select * from shadow_accounts").fetchall()
        conn.row_factory=sqlite3.Row
        accounts=conn.execute("select * from shadow_accounts").fetchall()
    with pytest.raises(ProtocolError,match="precedes"):
        verify_frozen_accounts(accounts,manifest,now=T20-timedelta(days=1))
    manifest.unlink()
    with pytest.raises(ProtocolError,match="does not exist"):
        run_cycle(db,[ASSET],now=AFTER,fetch_fn=lambda _:pytest.fail("read"),
                  protocol_path=manifest)


def test_missed_first_day_cannot_be_backfilled_after_restart(study):
    db,manifest,_=study
    future=datetime(2026,10,24,1,21,tzinfo=timezone.utc)
    # At most current next-day ticker is used; never forge missed Oct 21-22.
    price=float(bars("2026-10-23")["close"].iloc[-1])
    res=run_cycle(db,[ASSET],now=future,
        fetch_fn=lambda _:bars("2026-10-23"),
        quote_fn=lambda p,s,*,now:(price,now.date().isoformat()),
        protocol_path=manifest,
    )
    assert res["status"]=="OK"
    coverage=assess_collection(db,manifest,now=future)
    assert coverage["status"]=="BREACH_OR_MISSING"
    assert coverage["expected_due_days_per_account"]==3
    assert all(t["observed_due_days"]==1 and t["missing_due_days"]==2 for t in coverage["accounts"])
    with sqlite3.connect(db) as conn:
        assert {r[0] for r in conn.execute("select distinct decision_date from shadow_events")}=={"2026-10-23"}


def test_watchdog_protocol_alert_transition_only_on_due_date(study):
    db,manifest,tmp_path=study
    state=tmp_path/"watchdog.db"
    args={"shadow_db":db,"state_db":state,"report_dir":tmp_path/"forward",
          "windows_notifications":False,"health_check":lambda:True,
          "protocol_path":manifest}
    early=check(now=BEFORE,**args)
    assert early["status"]=="OK"
    assert early["protocol_compliance"]["status"]=="REGISTERED_WAITING_FIRST_DUE"
    assert not any(t["code"]=="PROTOCOL_DUE_OBSERVATIONS_MISSING" for t in early["alert_transitions"])
    late=check(now=AFTER,**args)
    assert late["status"]=="OK"
    assert sum(t["code"]=="PROTOCOL_DUE_OBSERVATIONS_MISSING" for t in late["alert_transitions"])==1
    assert check(now=AFTER+timedelta(minutes=4),**args)["alert_transitions"]==[]
    assert late["protocol_compliance"]["eligible_for_promotion"] is False


def test_bad_protocol_hash_causes_critical_operational_alert(study):
    db,manifest,tmp_path=study
    obj=json.loads(manifest.read_text())
    obj["required_complete_utc_days"]=90
    manifest.write_text(json.dumps(obj))
    result=check(shadow_db=db,state_db=tmp_path/"alerts.db",
                 report_dir=tmp_path/"forward",now=T20,
                 windows_notifications=False,health_check=lambda:True,
                 protocol_path=manifest)
    assert result["status"]=="DEGRADED"
    assert any(t["code"]=="PROTOCOL_CHECK_FAILED" for t in result["alert_transitions"])
    assert result["protocol_compliance"]["status"]=="UNAVAILABLE"


def test_local_api_exposes_only_coverage_not_pnl(study):
    db,manifest,_=study
    server=ThreadingHTTPServer(("127.0.0.1",0),make_handler(db,protocol_file=manifest))
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        url=f"http://127.0.0.1:{server.server_address[1]}/api/protocol-collection"
        with urlopen(url,timeout=5) as res:
            assert res.status==200
            obj=json.load(res)
        assert obj["includes_return_metrics"] is False
        assert obj["real_orders_enabled"] is False
        assert not any(x in str(obj) for x in ("log_excess","observed_net_return","last_equity","cash"))
    finally:
        server.shutdown();thread.join(timeout=3);server.server_close()


def test_watchdog_writes_coverage_only_and_blocks_broken_protocol_report(study):
    db,manifest,tmp_path=study
    report_dir=tmp_path/"daily"
    output=check(shadow_db=db,state_db=tmp_path/"watchdog.db",
                 report_dir=report_dir,now=BEFORE,
                 windows_notifications=False,health_check=lambda:True,
                 protocol_path=manifest)
    assert output["status"]=="OK"
    doc=json.loads((report_dir/"2026-10-22.json").read_text())
    assert doc["protocol_collection_compliance"]["includes_return_metrics"] is False
    assert doc["protocol_collection_compliance"]["status"]=="REGISTERED_WAITING_FIRST_DUE"
    manifest_data=json.loads(manifest.read_text())
    manifest_data["required_complete_utc_days"]=800
    manifest.write_text(json.dumps(manifest_data))
    different_day=check(shadow_db=db,state_db=tmp_path/"watchdog.db",
                        report_dir=report_dir,now=AFTER,
                        windows_notifications=False,health_check=lambda:True,
                        protocol_path=manifest)
    assert different_day["status"]=="DEGRADED"
    assert not different_day["reports"]
    assert any(a["code"]=="PROTOCOL_CHECK_FAILED" for a in different_day["alert_transitions"])


def test_halted_virtual_track_never_counts_as_clean_collection(study):
    db,manifest,_=study
    with sqlite3.connect(db) as con:
        # A halt without an event is ledger tampering and should fail its audit,
        # not be silently treated as a completed protocol.
        con.execute("UPDATE shadow_accounts SET halted=1 WHERE label='buy_hold_20pct'")
    result=assess_collection(db,manifest,now=AFTER)
    assert result["status"]=="INVALID_LEDGER"
    assert not result["eligible_for_promotion"]


def test_isolated_watchdog_state_does_not_replace_production_alert_snapshot(study):
    db,manifest,tmp_path=study
    state=tmp_path/"watchdog-test-isolated.db"
    canonical=tmp_path/"watchdog-history.json"
    canonical.write_text('{"events":[{"code":"PRESERVE_ME"}]}')
    output=check(shadow_db=db,state_db=state,report_dir=tmp_path/"reports",
                 now=BEFORE,windows_notifications=False,
                 health_check=lambda:True,protocol_path=manifest)
    assert output["status"]=="OK"
    assert canonical.read_text()=='{"events":[{"code":"PRESERVE_ME"}]}'
    isolated=tmp_path/"watchdog-test-isolated-history.json"
    assert isolated.exists()
    assert output["inbox_snapshot"]==str(isolated)

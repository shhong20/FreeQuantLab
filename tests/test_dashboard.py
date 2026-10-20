"""Read-only dashboard and anomaly monitoring regression tests."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
import json
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from quantlab import shadow
from quantlab.dashboard import make_handler, serve
from quantlab.monitor import snapshot
from quantlab.data import demo_daily
import pandas as pd
from quantlab.settings import Settings


ASSET = {"provider":"upbit","symbol":"KRW-BTC","kind":"crypto","currency":"KRW","bars":320}
D20 = datetime(2026,10,20,3,tzinfo=timezone.utc)
D21EARLY = datetime(2026,10,21,0,1,tzinfo=timezone.utc)
D21LATE = datetime(2026,10,21,1,21,tzinfo=timezone.utc)


def bars(day):
    b=demo_daily(320)
    b["date"]=pd.date_range(end=day,periods=320)
    return b


def database(tmp_path):
    path=tmp_path/"shadow.db"
    shadow.bootstrap(path,[ASSET],Settings(),now=D20,
                     fetch_fn=lambda _:bars("2026-10-19"))
    return path


def test_real_incubation_snapshot_no_pretend_performance_or_disk_changes(tmp_path):
    path=database(tmp_path)
    old=path.stat().st_mtime_ns
    snap=snapshot(path,now=D20)
    assert snap["real_orders_enabled"] is False
    assert snap["automatic_promotion_enabled"] is False
    assert snap["summary"]["tracks"]==3
    assert snap["summary"]["recorded_events"]==0
    assert not snap["alerts"]
    assert all(t["state"]=="incubating" for t in snap["tracks"])
    assert all(t["observed_return"] is None for t in snap["tracks"])
    assert all(t["max_recorded_drawdown"] is None for t in snap["tracks"])
    assert all(t["promotion_eligible"] is False for t in snap["tracks"])
    assert path.stat().st_mtime_ns == old


def test_after_deadline_new_decision_missing_is_warning(tmp_path):
    path=database(tmp_path)
    before=snapshot(path,now=D21EARLY)
    assert not any(a["code"]=="DECISION_MISSING" for a in before["alerts"])
    assert all(t["state"]=="awaiting_cycle" for t in before["tracks"])
    late=snapshot(path,now=D21LATE)
    assert late["summary"]["warnings"]>=3
    assert sum(a["code"]=="DECISION_MISSING" for a in late["alerts"])==3
    assert any(a["code"]=="NO_CYCLE_TODAY" for a in late["alerts"])
    assert all(t["state"]=="overdue" for t in late["tracks"])


def test_live_forward_observations_are_accurate_and_no_missing_alarm(tmp_path):
    path=database(tmp_path)
    result=shadow.run_cycle(path,[ASSET],now=D21EARLY,
        fetch_fn=lambda _:bars("2026-10-20"),
        quote_fn=lambda provider,symbol,*,now:(100.0,now.date().isoformat()))
    assert result["status"]=="OK"
    snap=snapshot(path,now=D21LATE)
    assert len(snap["recent_events"])==3
    assert all(t["events"]==1 and t["state"]=="recorded" for t in snap["tracks"])
    assert snap["summary"]["last_run_status"]=="OK"
    assert len(snap["recent_runs"])==1
    assert len(snap["recent_events"])==snap["summary"]["recorded_events"]
    assert not any(a["code"]=="DECISION_MISSING" for a in snap["alerts"])
    assert sum(t["simulated_orders"] for t in snap["tracks"]) == snap["summary"]["virtual_orders"]


def test_halted_account_and_failed_run_produce_alerts(tmp_path):
    path=database(tmp_path)
    with sqlite3.connect(path) as con:
        con.execute("UPDATE shadow_accounts SET halted=1 WHERE label='trend_ma20_80'")
        con.execute("INSERT INTO shadow_runs (created_at,status,details_json) VALUES (?,?,?)",(
            D20.isoformat(),"ERROR",json.dumps({
                "errors":[{"asset":"KRW-BTC","error":"public quote API unreachable"}]
            })
        ))
    data=snapshot(path,now=D20)
    assert any(a["code"]=="RISK_HALTED" for a in data["alerts"])
    assert any(a["code"]=="LATEST_RUN_FAILED" for a in data["alerts"])
    assert data["summary"]["halted"]==1
    assert data["summary"]["failures_last_7d_in_latest_100_runs"]==1


def test_missing_database_rejected_without_creating_file(tmp_path):
    path=tmp_path/"nonexistent.db"
    with pytest.raises(FileNotFoundError):
        snapshot(path,now=D20)
    assert not path.exists()


def test_reject_bad_now_datetime(tmp_path):
    path=database(tmp_path)
    with pytest.raises(ValueError,match="Timezone-aware"):
        snapshot(path,now=datetime(2026,10,20,10))


@pytest.fixture()
def server(tmp_path):
    from http.server import ThreadingHTTPServer
    path=database(tmp_path)
    httpd=ThreadingHTTPServer(("127.0.0.1",0),make_handler(path))
    thread=threading.Thread(target=httpd.serve_forever,daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()


def test_readonly_http_dashboard_serves_local_static_html_css_js(server):
    for route, expected in [
        ("/","text/html"),("/styles.css","text/css"),
        ("/app.js","application/javascript"),("/api/status","application/json"),
        ("/healthz","application/json"),
    ]:
        with urlopen(server+route,timeout=4) as result:
            assert result.status==200
            assert expected in result.headers["Content-Type"]
            assert result.headers["Cache-Control"]=="no-store"
            assert "script-src 'self'" in result.headers["Content-Security-Policy"]
            assert result.headers["X-Content-Type-Options"]=="nosniff"
            data=result.read()
            assert data
            if route=="/api/status":
                parsed=json.loads(data)
                assert len(parsed["tracks"])==3
                assert parsed["real_orders_enabled"] is False


def test_http_mutations_never_accepted(server):
    for method in ("POST","PUT","DELETE"):
        req=Request(server+"/api/status",data=b'{"halted":false}',method=method)
        with pytest.raises(HTTPError) as e:
            urlopen(req,timeout=4)
        assert e.value.code==405
    for bad_path in ("/../../../etc/passwd","/quantlab/shadow.py","/api/activate"):
        try:
            with urlopen(server+bad_path,timeout=4) as r:
                assert r.status==404
        except HTTPError as e:
            assert e.code==404


def test_dashboard_bind_nonloopback_is_rejected(tmp_path):
    with pytest.raises(ValueError,match="loopback"):
        serve(db=tmp_path/"x.db",bind="0.0.0.0",port=8788)
    with pytest.raises(ValueError,match="loopback"):
        serve(db=tmp_path/"x.db",bind="192.168.1.15",port=8788)
    with pytest.raises(ValueError,match="loopback IP"):
        serve(db=tmp_path/"x.db",bind="example.com",port=8788)


def test_dns_rebinding_host_header_is_refused(server):
    req=Request(server+"/api/status",headers={"Host":"arbitrary.example:8788"})
    with pytest.raises(HTTPError) as error:
        urlopen(req,timeout=4)
    assert error.value.code==403


def test_server_static_has_no_external_deps_or_untrusted_innerhtml():
    from quantlab.dashboard import STATIC
    html=(STATIC/"dashboard.html").read_text(encoding="utf-8")
    js=(STATIC/"app.js").read_text(encoding="utf-8")
    css=(STATIC/"styles.css").read_text(encoding="utf-8")
    assert "https://" not in html+js+css
    assert ".innerHTML" not in js
    assert "fetch(\"/api/status\"" in js
    assert "api/activate" not in html+js
    assert '[hidden]{display:none!important}' in css



def test_report_and_alert_inbox_are_readonly_http_endpoints(tmp_path):
    from http.server import ThreadingHTTPServer
    from quantlab.forward_reports import report, save_report
    from quantlab.watchdog import persist_alert_transitions
    from datetime import datetime, timezone
    import sqlite3

    path=database(tmp_path)
    report_dir=tmp_path/"forward"
    state=tmp_path/"watchdog.db"
    daily=report(path,now=datetime(2026,10,20,3,tzinfo=timezone.utc))
    save_report(daily,report_dir)
    persist_alert_transitions(
        state, [{"severity":"warning","code":"TEST_WARNING",
                 "message":"test inbox","symbol":"KRW-BTC"}],
        now=datetime(2026,10,20,3,tzinfo=timezone.utc),
    )
    httpd=ThreadingHTTPServer(
        ("127.0.0.1",0),
        make_handler(path,reports_dir=report_dir,alerts_db=state),
    )
    thread=threading.Thread(target=httpd.serve_forever,daemon=True)
    thread.start()
    try:
        base=f"http://127.0.0.1:{httpd.server_address[1]}"
        with urlopen(base+"/api/latest-report",timeout=4) as response:
            data=json.load(response)
            assert response.status==200
            assert data["report_date_kst"]=="2026-10-20"
            assert data["summary"]["tracks"]==3
            assert not data["real_orders_enabled"]
        with urlopen(base+"/api/alert-history",timeout=4) as response:
            inbox=json.load(response)
            assert inbox["events"][0]["code"]=="TEST_WARNING"
            assert inbox["events"][0]["kind"]=="triggered"
        with sqlite3.connect(state) as conn:
            assert conn.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0]==1
    finally:
        httpd.shutdown();thread.join(timeout=3);httpd.server_close()


def test_missing_report_and_missing_inbox_are_safe(tmp_path):
    from http.server import ThreadingHTTPServer
    path=database(tmp_path)
    httpd=ThreadingHTTPServer(
        ("127.0.0.1",0),
        make_handler(path,reports_dir=tmp_path/"empty",alerts_db=tmp_path/"no.db")
    )
    thread=threading.Thread(target=httpd.serve_forever,daemon=True)
    thread.start()
    try:
        base=f"http://127.0.0.1:{httpd.server_address[1]}"
        with pytest.raises(HTTPError) as e:
            urlopen(base+"/api/latest-report",timeout=4)
        assert e.value.code==404
        with urlopen(base+"/api/alert-history",timeout=4) as response:
            assert json.load(response)=={"events":[]}
        assert not (tmp_path/"no.db").exists()
    finally:
        httpd.shutdown();thread.join(timeout=3);httpd.server_close()

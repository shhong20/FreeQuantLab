"""Cross-lane coverage: NO returns before 90-day preregistered cutoff."""
from __future__ import annotations

from datetime import datetime,timedelta,timezone,date
import sqlite3

import numpy as np
import pandas as pd
import pytest

from quantlab.agent_lab import FactorProposal,LocalLLMAgent
from quantlab.candidate_lab import propose_lab,register_lab as register_agent
from quantlab.data import demo_daily
from quantlab.lane_comparison import compare_three_lanes
from quantlab.prospective import lock_protocol
from quantlab.qlib_lab import create_plan,register_lab as register_qlib
from quantlab.settings import Settings
from quantlab.shadow import bootstrap

T20=datetime(2026,10,20,4,tzinfo=timezone.utc)
NEXT=datetime(2026,10,22,1,20,tzinfo=timezone.utc)
END=datetime(2027,1,19,1,30,tzinfo=timezone.utc)
ASSETS=[{"provider":"upbit","symbol":x,"currency":"KRW",
         "kind":"crypto","bars":320} for x in ("KRW-BTC","KRW-ETH")]


def bars(last="2026-10-19"):
    out=demo_daily(320)
    out["date"]=pd.date_range(end=last,periods=len(out))
    return out


class StubLocal(LocalLLMAgent):
    def __init__(self):
        super().__init__("http://127.0.0.1:11434/v1","offline-stub")
    def propose(self,training_summary):
        return [FactorProposal("test","r5","gt",0.)]


@pytest.fixture()
def rig(tmp_path,monkeypatch):
    from quantlab import qlib_lab
    from quantlab import lane_comparison
    def fake_model(path):
        symbol=str(path).split("/")[-1]
        a=next(x for x in ASSETS if x["symbol"]==symbol)
        return {"registered_at_utc":T20.isoformat(),
                "training_last_completed_utc_day":"2026-10-19",
                "settings":__import__("dataclasses").asdict(Settings()),
                "asset":a,
                "sha256":"a"*64 if symbol=="KRW-BTC" else "b"*64,
                "model_sha256":"c"*64 if symbol=="KRW-BTC" else "d"*64},object()
    monkeypatch.setattr(qlib_lab,"read_qlib_bundle",fake_model)
    # Isolated synthetic fixture has no actual model; authentic model replay
    # is independently covered in test_qlib_lab and test_qlib_frozen.
    monkeypatch.setattr(lane_comparison,"audit_decisions",
                        lambda *a,**k:{"status":"AWAITING_FIRST_DECISION"})
    original=tmp_path/"shadow.db"
    exaone=tmp_path/"agent_lab_shadow.db"
    qdb=tmp_path/"qlib_lab_shadow.db"
    proto=tmp_path/"protocol.json"
    aplan=tmp_path/"agent.json"
    qplan=tmp_path/"qlib.json"
    bootstrap(original,ASSETS,Settings(),now=T20,fetch_fn=lambda _:bars())
    lock_protocol(original,proto,now=T20,days=90)
    propose_lab(aplan,assets=ASSETS,settings=Settings(),agent=StubLocal(),
                now=T20,fetch_fn=lambda _:bars())
    register_agent(aplan,exaone,now=T20,fetch_fn=lambda _:bars(),
                   production_db=original)
    create_plan(qplan,[tmp_path/"KRW-BTC",tmp_path/"KRW-ETH"],now=T20)
    register_qlib(qplan,qdb,now=T20,fetch_fn=lambda _:bars(),
                  production_db=original,agent_db=exaone)
    return original,exaone,qdb,proto,aplan,qplan


def compare(rig,when):
    return compare_three_lanes(*rig,now=when)


def test_ten_independent_accounts_locked_no_outcomes_before_cutoff(rig):
    x=compare(rig,T20)
    assert x["status"]=="WAITING_FOR_FUTURE_CUTOFF",x["issues"]
    assert x["registered_accounts"]==10
    assert len(x["coverage"])==10
    assert x["due_dates_per_account"]==0
    assert x["net_return_by_strategy"] is None
    assert not x["real_orders_enabled"]


def test_missing_due_first_day_is_coverage_only(rig):
    before=compare(rig,NEXT-timedelta(minutes=1))
    after=compare(rig,NEXT)
    assert before["due_dates_per_account"]==0
    assert after["due_dates_per_account"]==1
    assert all(x["missing_due_dates"]==1 for x in after["coverage"])
    assert after["net_return_by_strategy"] is None


def test_cutoff_with_no_real_observations_refuses_comparison(rig):
    report=compare(rig,END)
    assert report["status"]=="INCOMPLETE_OR_NOT_COMPARABLE"
    assert "MISSING_90_DAY_OBSERVATIONS_OR_RISK_HALT" in report["issues"]
    assert report["net_return_by_strategy"] is None
    assert all(x["missing_due_dates"]==90 for x in report["coverage"])


def insert_synthetic_zero_return_days(rig,*,skew=False):
    """Offline-only fixture, never against an actual WSL account DB."""
    for i,path in enumerate(rig[:3]):
        with sqlite3.connect(path) as con:
            accounts=con.execute("SELECT track_id,symbol,initial_cash FROM shadow_accounts").fetchall()
            for track,sym,money in accounts:
                for n in range(90):
                    decision=date(2026,10,21)+timedelta(days=n)
                    session=decision+timedelta(days=1)
                    px=105. if skew and i==1 else 100.
                    con.execute(
                        "INSERT INTO shadow_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (track,decision.isoformat(),session.isoformat(),
                         "HOLD",0,px,0.,0.,money,0.,money,
                         session.isoformat()+"T00:15:00+00:00"),
                    )
                con.execute(
                    "UPDATE shadow_accounts SET day_date=?,last_equity=? "
                    "WHERE track_id=?",
                    ("2027-01-19",money,track),
                )

            for sym in ("KRW-BTC","KRW-ETH"):
                for n in range(90):
                    decision=date(2026,10,21)+timedelta(days=n)
                    session=decision+timedelta(days=1)
                    quote=105. if skew and i==1 else 100.
                    con.execute("INSERT INTO shadow_quote_evidence VALUES(?,?,?,?,?,?,?)",
                        (sym,decision.isoformat(),session.isoformat(),
                         quote,quote,session.isoformat()+"T00:15:00+00:00",
                         "upbit_public_ticker"))


def test_complete_synthetic_same_day_parity_after_cutoff_all_zero_returns(rig):
    insert_synthetic_zero_return_days(rig)
    x=compare(rig,END)
    assert x["status"]=="DESCRIPTIVE_MANUAL_REVIEW_ONLY",x["issues"]
    assert x["same_day_cost_and_price_comparison"]
    assert len(x["net_return_by_strategy"])==10
    assert all(y["observed_90d_net_return"]==0 and
               y["excess_vs_same_symbol_buy_hold_pp"]==0
               for y in x["net_return_by_strategy"])
    assert x["blinded_or_confirmatory"] is False
    assert not x["automatic_strategy_promotion"]


def test_even_complete_data_cannot_leak_results_before_cutoff(rig):
    insert_synthetic_zero_return_days(rig)
    x=compare(rig,T20)
    assert x["net_return_by_strategy"] is None
    assert x["status"]=="WAITING_FOR_FUTURE_CUTOFF"


def test_same_dates_but_unequal_quote_prices_withheld(rig):
    insert_synthetic_zero_return_days(rig,skew=True)
    x=compare(rig,END)
    assert x["net_return_by_strategy"] is None
    assert "DIFFERENT_OBSERVATION_PRICE_BASIS" in x["issues"]


def test_missing_one_lane_day_cannot_be_replaced_by_other_lanes(rig):
    insert_synthetic_zero_return_days(rig)
    with sqlite3.connect(rig[1]) as con:
        # Deleting a mid-stream paper event changes provenance but does not
        # give permission to copy or manufacture missing quotes.
        track=con.execute("SELECT track_id FROM shadow_accounts LIMIT 1").fetchone()[0]
        con.execute("DELETE FROM shadow_events WHERE track_id=? AND decision_date=?",
                    (track,"2026-11-01"))
    x=compare(rig,END)
    assert x["net_return_by_strategy"] is None
    assert x["status"]=="INCOMPLETE_OR_NOT_COMPARABLE"


def test_local_dashboard_crosslane_api_returns_only_coverage(rig):
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from urllib.request import urlopen,Request
    from urllib.error import HTTPError
    from quantlab.dashboard import make_handler
    import json
    server=ThreadingHTTPServer(("127.0.0.1",0),make_handler(
        rig[0],protocol_file=rig[3],agent_db=rig[1],qlib_db=rig[2],
        agent_plan=rig[4],qlib_plan=rig[5]))
    thread=Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        base=f"http://127.0.0.1:{server.server_address[1]}"
        with urlopen(base+"/api/research-lanes",timeout=5) as response:
            assert response.status==200
            body=json.load(response)
        assert body["registered_accounts"]==10
        assert body["net_return_by_strategy"] is None
        assert body["real_orders_enabled"] is False
        with pytest.raises(HTTPError) as er:
            urlopen(Request(base+"/api/research-lanes",data=b"{}",method="POST"),timeout=5)
        assert er.value.code==405
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_complete_events_without_saved_public_quote_not_comparable(rig):
    insert_synthetic_zero_return_days(rig)
    with sqlite3.connect(rig[2]) as con:
        con.execute("DELETE FROM shadow_quote_evidence WHERE symbol='KRW-BTC' AND decision_date='2026-10-25'")
    x=compare(rig,END)
    assert x["net_return_by_strategy"] is None
    assert "MISSING_OR_CONTRADICTORY_PUBLIC_QUOTE" in x["issues"]

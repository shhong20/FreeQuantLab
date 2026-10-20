"""Isolated Agent factor plan must never mutate the registered 90-day experiment."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pandas as pd
import pytest

from quantlab.agent_lab import FactorProposal, LocalLLMAgent
from quantlab.candidate_lab import (
    CandidateLabError, propose_lab, read_lab_plan, register_lab,run_lab_cycle
)
from quantlab.data import demo_daily
from quantlab.settings import Settings

T20=datetime(2026,10,20,2,30,tzinfo=timezone.utc)
T21=datetime(2026,10,21,0,15,tzinfo=timezone.utc)
ASSET={"provider":"upbit","symbol":"KRW-BTC","kind":"crypto","currency":"KRW","bars":320}


def candles(day):
    frame=demo_daily(320)
    frame["date"]=pd.date_range(end=day,periods=len(frame))
    return frame


class StubLocal(LocalLLMAgent):
    def __init__(self):
        super().__init__("http://127.0.0.1:11434/v1","stub-local-only")
    def propose(self, training_summary):
        assert "test" not in training_summary
        assert training_summary["bar_count"]==320
        return [FactorProposal("anything","r5","gt",0.)]


@pytest.fixture()
def frozen(tmp_path):
    source=tmp_path/"shadow.db"
    # Seed production with an independent frozen account for invariance check.
    from quantlab.shadow import bootstrap
    bootstrap(source,[ASSET],Settings(),now=T20,
              fetch_fn=lambda _:candles("2026-10-19"))
    dest=tmp_path/"candidate-plan.json"
    plan=propose_lab(dest,assets=[ASSET],settings=Settings(),
                     agent=StubLocal(),now=T20,
                     fetch_fn=lambda _:candles("2026-10-19"))
    return source,dest,tmp_path/"agent_lab_shadow.db",plan


def test_candidate_plan_is_single_use_hashed_and_no_auto_trading(frozen):
    source,plan,lab,item=frozen
    assert item["automatic_runner_enabled"] is False
    assert item["qlib_model_execution_enabled"] is False
    assert item["broker_orders_enabled"] is False
    assert item["eligible_for_promotion"] is False
    assert item["entries"][0]["spec"]["kind"]=="factor"
    assert item["entries"][0]["proposals_evaluated_on_oos"]==0
    assert read_lab_plan(plan)["sha256"]==item["sha256"]
    with pytest.raises(CandidateLabError,match="already frozen"):
        propose_lab(plan,assets=[ASSET],settings=Settings(),agent=StubLocal(),now=T20,
                    fetch_fn=lambda _:candles("2026-10-19"))


def test_plan_hash_rejects_config_mutation(frozen):
    _,plan,_,_=frozen
    d=json.loads(plan.read_text())
    d["entries"][0]["spec"]["threshold"]=.24
    plan.write_text(json.dumps(d))
    with pytest.raises(CandidateLabError,match="hash changed"):
        read_lab_plan(plan)


def test_production_db_cannot_be_reused_and_registration_must_be_same_utc_day(frozen):
    source,plan,lab,_=frozen
    with pytest.raises(CandidateLabError,match="production"):
        register_lab(plan,source,now=T20,production_db=source,
                     fetch_fn=lambda _:candles("2026-10-19"))
    with pytest.raises(CandidateLabError,match="same UTC date"):
        register_lab(plan,lab,now=T21,production_db=source,
                     fetch_fn=lambda _:candles("2026-10-20"))
    assert not lab.exists()


def test_isolated_lab_bootstrap_and_forward_once_preserves_production(frozen):
    source,plan,lab,_=frozen
    with sqlite3.connect(source) as con:
        before=(con.execute("SELECT * FROM shadow_accounts ORDER BY track_id").fetchall(),
                con.execute("SELECT * FROM shadow_events ORDER BY track_id").fetchall())
    x=register_lab(plan,lab,now=T20,production_db=source,
                   fetch_fn=lambda _:candles("2026-10-19"))
    assert x["status"]=="REGISTERED" and x["created"]==1
    with sqlite3.connect(lab) as con:
        assert con.execute("select count(*) from shadow_accounts").fetchone()[0]==1
        assert con.execute("select count(*) from shadow_events").fetchone()[0]==0
    a=run_lab_cycle(plan,lab,now=T20,production_db=source,
                    fetch_fn=lambda _:candles("2026-10-19"),
                    quote_fn=lambda *args,**kwargs:pytest.fail("no new bar"))
    assert a["status"]=="OK" and a["automatic_timer"] is False
    price=float(candles("2026-10-20")["close"].iloc[-1])
    b=run_lab_cycle(plan,lab,now=T21,production_db=source,
                    fetch_fn=lambda _:candles("2026-10-20"),
                    quote_fn=lambda p,s,*,now:(price,now.date().isoformat()))
    assert b["status"]=="OK"
    assert b["lab_isolated_from_registered_90d"]
    assert sum(e.get("status")=="RECORDED" for e in b["results"])==1
    with sqlite3.connect(lab) as con:
        assert con.execute("select count(*) from shadow_events").fetchone()[0]==1
    with sqlite3.connect(source) as con:
        assert con.execute("select count(*) from shadow_events").fetchone()[0]==0
    with sqlite3.connect(source) as con:
        after=(con.execute("SELECT * FROM shadow_accounts ORDER BY track_id").fetchall(),
               con.execute("SELECT * FROM shadow_events ORDER BY track_id").fetchall())
    assert after==before


def test_isolated_lab_identity_drift_blocks_market_fetch(frozen):
    source,plan,lab,_=frozen
    register_lab(plan,lab,now=T20,production_db=source,
                 fetch_fn=lambda _:candles("2026-10-19"))
    with sqlite3.connect(lab) as con:
        con.execute("update shadow_accounts set spec_json=?",
                    ('{"label":"changed","kind":"hold"}',))
    with pytest.raises(CandidateLabError,match="factor changed"):
        run_lab_cycle(plan,lab,now=T21,production_db=source,
           fetch_fn=lambda _:pytest.fail("never fetch"))


def test_refuses_mock_agent_to_lock_production_proposal(tmp_path):
    from quantlab.agent_lab import MockAgent
    with pytest.raises(CandidateLabError,match="actual configured local"):
        propose_lab(tmp_path/"fake.json",assets=[ASSET],settings=Settings(),
                    agent=MockAgent(),now=T20,
                    fetch_fn=lambda _:candles("2026-10-19"))

"""Qlib virtual lab only: exact-model binding, no cross-ledger writes or timer."""
from __future__ import annotations

from datetime import datetime,timezone,timedelta
import sqlite3

import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.qlib_lab import (
    QlibLabError,create_plan,verify_plan,register_lab,run_manual,
)
from quantlab.shadow import run_cycle
from quantlab.settings import Settings

T20=datetime(2026,10,20,4,tzinfo=timezone.utc)
T21=T20+timedelta(days=1)
ASSETS=[{"provider":"upbit","symbol":sym,"currency":"KRW",
         "kind":"crypto","bars":240} for sym in ("KRW-BTC","KRW-ETH")]


def candles(day):
    d=demo_daily(240)
    d["date"]=pd.date_range(end=day,periods=len(d))
    return d


@pytest.fixture
def rig(tmp_path,monkeypatch):
    def fixed_model(path):
        symbol=str(path).split("/")[-1]
        if symbol not in ("KRW-BTC","KRW-ETH"):
            raise QlibLabError("Unknown model")
        a=next(x for x in ASSETS if x["symbol"]==symbol)
        return {
            "registered_at_utc":T20.isoformat(),
            "training_last_completed_utc_day":"2026-10-19",
            "settings":__import__("dataclasses").asdict(Settings()),
            "asset":a,
            "sha256":"a"*64 if symbol=="KRW-BTC" else "b"*64,
            "model_sha256":"c"*64 if symbol=="KRW-BTC" else "d"*64,
        },object()
    def inference(bundle,frame,*,now):
        body,_=fixed_model(bundle)
        return {
            "status":"READY","decision_utc_date":"2026-10-20",
            "bundle_manifest_sha256":body["sha256"],
            "model_sha256":body["model_sha256"],
            "probability":.65,"target":1,"threshold":.55,"real_orders_enabled":False,
        }
    monkeypatch.setattr("quantlab.qlib_lab.read_qlib_bundle",fixed_model)
    monkeypatch.setattr("quantlab.qlib_lab.predict_completed",inference)
    plan=tmp_path/"frozen.json"
    obj=create_plan(plan,[tmp_path/"KRW-BTC",tmp_path/"KRW-ETH"],now=T20)
    return tmp_path,plan,obj


def test_frozen_qlib_plan_is_write_once_hashed_and_validates_models(rig):
    root,plan,obj=rig
    assert len(obj["entries"])==2
    assert obj["retraining_allowed"] is False
    assert obj["automatic_timer"] is False
    assert not obj["real_orders_enabled"]
    assert verify_plan(plan)["sha256"]==obj["sha256"]
    with pytest.raises(QlibLabError,match="already exists"):
        create_plan(plan,[root/"KRW-BTC",root/"KRW-ETH"],now=T20)
    import json
    altered=__import__("json").loads(plan.read_text())
    altered["entries"][0]["spec"]["threshold"]=.75
    plan.write_text(json.dumps(altered))
    with pytest.raises(QlibLabError,match="hash changed"):
        verify_plan(plan)


def test_qlib_lab_register_and_manual_cycle_never_mutate_production(rig):
    root,plan,obj=rig
    db=root/"qlib_lab_shadow.db"
    source=root/"shadow.db"
    # Existing production db with other unrelated records remains untouched.
    with sqlite3.connect(source) as c:
        c.execute("CREATE TABLE production_invariant(x INTEGER)")
        c.execute("INSERT INTO production_invariant VALUES(7)")
    res=register_lab(plan,db,now=T20,fetch_fn=lambda _:candles("2026-10-19"),
                     production_db=source)
    assert res["accounts"]==2 and not res["automatic_timer"]
    out=run_manual(plan,db,now=T20,fetch_fn=lambda _:candles("2026-10-19"),
                   quote_fn=lambda *a,**k: pytest.fail("No new daily bar"),
                   production_db=source)
    assert out["status"]=="OK" and len(out["results"])==2
    price=float(candles("2026-10-20")["close"].iloc[-1])
    forward=run_manual(plan,db,now=T21,fetch_fn=lambda _:candles("2026-10-20"),
                    quote_fn=lambda p,s,*,now:(price,now.date().isoformat()),
                    production_db=source)
    assert forward["status"]=="OK",forward["errors"]
    assert sum(row["status"]=="RECORDED" for row in forward["results"])==2
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]==2
        assert c.execute("SELECT COUNT(*) FROM shadow_quote_evidence").fetchone()[0]==2
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT * FROM production_invariant").fetchall()==[(7,)]
    duplicate=run_manual(plan,db,now=T21,fetch_fn=lambda _:candles("2026-10-20"),
                    quote_fn=lambda *a,**k:pytest.fail("Idempotent; no quote request"),
                    production_db=source)
    assert duplicate["status"]=="OK"
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]==2


def test_lab_rejects_real_or_other_lane_and_model_checkpoint_drift(rig,monkeypatch):
    root,plan,obj=rig
    with pytest.raises(QlibLabError,match="dedicated"):
        register_lab(plan,root/"shadow.db",now=T20)
    with pytest.raises(QlibLabError,match="dedicated"):
        register_lab(plan,root/"agent_lab_shadow.db",now=T20)
    db=root/"qlib_lab_shadow.db"
    register_lab(plan,db,now=T20,fetch_fn=lambda _:candles("2026-10-19"))
    with pytest.raises(ValueError,match="isolated Qlib"):
        run_cycle(db,ASSETS,now=T21,
            fetch_fn=lambda _:pytest.fail("Generic runner must not fetch"))
    with sqlite3.connect(db) as c:
        c.execute("UPDATE shadow_accounts SET cash=cash+100 WHERE symbol='KRW-BTC'")
    with pytest.raises(QlibLabError,match="audit failed"):
        run_manual(plan,db,now=T21,
            fetch_fn=lambda _:pytest.fail("tampered ledger must not fetch"))


def test_model_mismatch_blocks_virtual_execution_before_fetch(rig,monkeypatch):
    root,plan,obj=rig
    db=root/"qlib_lab_shadow.db"
    register_lab(plan,db,now=T20,fetch_fn=lambda _:candles("2026-10-19"))
    def corrupted(path):
        body={"sha256":"e"*64,"model_sha256":"e"*64,
              "asset":next(x for x in ASSETS if x["symbol"]==str(path).split("/")[-1]),
              "training_last_completed_utc_day":"2026-10-19"}
        return body,object()
    monkeypatch.setattr("quantlab.qlib_lab.read_qlib_bundle",corrupted)
    with pytest.raises(QlibLabError,match="Checkpoint changed"):
        run_manual(plan,db,now=T21,fetch_fn=lambda _:pytest.fail("No data fetch"))

def test_no_retroactive_registration_or_silent_reinitialize(rig):
    root,plan,_=rig
    db=root/"qlib_lab_shadow.db"
    with pytest.raises(QlibLabError,match="original lock UTC date"):
        register_lab(plan,db,now=T21)
    register_lab(plan,db,now=T20,fetch_fn=lambda _:candles("2026-10-19"))
    with pytest.raises(QlibLabError,match="Never overwrite"):
        register_lab(plan,db,now=T20)

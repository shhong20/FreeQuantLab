"""Tests: real Qlib checkpoint, new process golden inference, corruption gates.

These may skip in normal lightweight CI; Qlib integration workflow executes
them with the real pyqlib + LightGBM training dependencies.
"""
from __future__ import annotations

from datetime import datetime,timezone,timedelta
import json
import subprocess
import sys

import pandas as pd
import pytest

pytest.importorskip("qlib")
pytest.importorskip("lightgbm")

from quantlab.qlib_frozen import (
    FrozenQlibError, freeze_qlib_bundle, read_qlib_bundle, predict_completed
)
from quantlab.data import demo_daily
from quantlab.settings import Settings


NOW=datetime(2026,10,20,2,0,tzinfo=timezone.utc)
ASSET={"provider":"upbit","symbol":"KRW-BTC","currency":"KRW","kind":"crypto","bars":240}


def candles(last="2026-10-19"):
    d=demo_daily(240)
    d["date"]=pd.date_range(end=last,periods=len(d))
    return d


@pytest.fixture
def frozen(tmp_path):
    bundle=tmp_path/"models"/"KRW-BTC"
    model=freeze_qlib_bundle(bundle,asset=ASSET,settings=Settings(),
                             now=NOW,fetch_fn=lambda a:candles(),
                             training_state=tmp_path/"training")
    return bundle,model


def test_actual_qlib_train_checkpoint_and_fresh_process(frozen):
    bundle,manifest=frozen
    assert manifest["train_known_labels"]>=80
    assert manifest["validation_known_labels"]>=25
    assert manifest["last_two_labels_excluded"] is True
    assert manifest["threshold_chosen_from_test"] is False
    info,model=read_qlib_bundle(bundle)
    assert info["sha256"]==manifest["sha256"]
    proc=subprocess.run([sys.executable,"-c",
        "from quantlab.qlib_frozen import read_qlib_bundle; import sys; m,x=read_qlib_bundle(sys.argv[1]); print(m['sha256'])",
        str(bundle)],capture_output=True,text=True,timeout=35)
    assert proc.returncode==0,proc.stderr
    assert proc.stdout.strip().splitlines()[-1]==manifest["sha256"]


def test_model_file_golden_or_feature_schema_tampering_rejected(frozen,monkeypatch):
    bundle,manifest=frozen
    model=bundle/"model.txt"
    data=model.read_bytes()
    model.write_bytes(data+b"\n# modified checkpoint")
    with pytest.raises(FrozenQlibError,match="Checkpoint bytes"):
        read_qlib_bundle(bundle)
    model.write_bytes(data)
    golden=bundle/"golden.json"
    saved=golden.read_text()
    golden.write_text(saved.replace("[", "[0.2,",1))
    with pytest.raises(FrozenQlibError,match="Golden"):
        read_qlib_bundle(bundle)
    golden.write_text(saved)
    monkeypatch.setattr("quantlab.qlib_frozen._feature_source_digest",lambda:"00"*32)
    with pytest.raises(FrozenQlibError,match="Feature schema"):
        read_qlib_bundle(bundle)


def test_no_retrain_no_future_results_before_new_daily_bar(frozen):
    bundle,manifest=frozen
    r=predict_completed(bundle,candles(),now=NOW)
    assert r["status"]=="WAIT_NEW_COMPLETED_UTC_BAR"
    assert r["real_orders_enabled"] is False
    # Offline future fixture only. Never writes to real-market paper accounts.
    nxt=predict_completed(bundle,candles("2026-10-20"),
                          now=NOW+timedelta(days=1))
    assert nxt["status"]=="READY"
    assert nxt["training_last_completed_utc_day"]=="2026-10-19"
    assert 0<=nxt["probability"]<=1
    assert nxt["target"] in (0,1)
    assert nxt["retrained"] is False


def test_cannot_refreeze_same_bundle_and_must_use_completed_candle(frozen,tmp_path):
    bundle,_=frozen
    with pytest.raises(FrozenQlibError,match="already exists"):
        freeze_qlib_bundle(bundle,asset=ASSET,settings=Settings(),
                           now=NOW,fetch_fn=lambda _:candles(),
                           training_state=tmp_path/"training")
    with pytest.raises(ValueError,match="stale"):
        freeze_qlib_bundle(tmp_path/"bad",asset=ASSET,settings=Settings(),
                           now=NOW,fetch_fn=lambda _:candles("2026-10-18"),
                           training_state=tmp_path/"training")


def test_git_versioned_real_market_model_bundles_reload_with_exact_dependencies():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    for symbol in ("KRW-BTC","KRW-ETH"):
        manifest,booster=read_qlib_bundle(root/"models"/"qlib"/symbol)
        assert manifest["asset"]["symbol"]==symbol
        assert manifest["training_last_completed_utc_day"]=="2026-10-19"
        assert booster.num_trees()>=1
        assert manifest["real_orders_enabled"] is False


def test_real_committed_model_two_asset_next_day_virtual_decision_replays(tmp_path):
    """Actual immutable BTC+ETH Qlib boosters; completely synthetic future bar.

    This tests software reproducibility and simulated ledger consistency,
    NEVER observed trading returns, live fills or AI investment success.
    """
    from pathlib import Path
    from quantlab.qlib_lab import create_plan,register_lab,run_manual,verify_plan
    from quantlab.qlib_decision_audit import audit_decisions
    root=Path(__file__).resolve().parents[1]
    source=[root/"models"/"qlib"/sym for sym in ("KRW-BTC","KRW-ETH")]
    stamp=datetime(2026,10,20,8,30,tzinfo=timezone.utc)
    nxt=stamp+timedelta(days=1)
    manifest=tmp_path/"lab.json"
    db=tmp_path/"qlib_lab_shadow.db"
    obj=create_plan(manifest,source,now=stamp)
    assert obj["automatic_timer"] is False
    register_lab(manifest,db,now=stamp,fetch_fn=lambda _:candles())
    upcoming=candles("2026-10-20")
    quote=float(upcoming["close"].iloc[-1])
    out=run_manual(manifest,db,now=nxt,fetch_fn=lambda _:upcoming,
                  quote_fn=lambda p,s,*,now:(quote,now.date().isoformat()),
                  production_db=tmp_path/"shadow.db",
                  agent_db=tmp_path/"agent_lab_shadow.db")
    assert out["status"]=="OK",out["errors"]
    assert all(item["status"]=="RECORDED" for item in out["results"])
    frozen=verify_plan(manifest)
    validated=audit_decisions(db,frozen)
    assert validated["status"]=="PASS",validated["issues"]
    assert validated["qlib_model_events"]==2
    assert validated["fully_replayed_decisions"]==2
    assert not validated["actual_trade_execution_verified"]
    assert not validated["broker_orders_enabled"]

"""Tests of historical maxT diagnostics and strictly future preregistration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.diagnostics import exploratory_familywise_max_t, diagnostic_summary
from quantlab.prospective import lock_protocol, inspect_protocol, read_protocol, ProtocolError
from quantlab.shadow import bootstrap
from quantlab.settings import Settings


START = datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc)
ASSET = {"provider":"upbit","symbol":"KRW-BTC","kind":"crypto",
         "currency":"KRW","bars":320}


def test_joint_bootstrap_adjustment_monotonic_reproducible_and_no_promotion():
    rng = np.random.default_rng(2026)
    folds=[]
    for _ in range(4):
        base=rng.normal(0.0002,0.008,125)
        noise=rng.normal(0,0.004,125)
        folds.append({
            "buy_hold_same_exposure":base,
            "A_baseline":base+noise+0.0004,
            "B_agent":base+noise*1.05+0.0003,
            "C_agent_review":base-noise*0.1,
            "buy_hold_riskmatched_C":base*.5,
        })
    a=exploratory_familywise_max_t(folds,repetitions=200,seed=314)
    b=exploratory_familywise_max_t(folds,repetitions=200,seed=314)
    assert a==b
    assert a["candidate_count"]==3
    assert "buy_hold_riskmatched_C" not in a["candidate_family"]
    assert not a["confirmatory_p_values"]
    assert not a["adjusts_for_previously_screened_candidates"]
    assert a["past_oos_already_inspected"]
    for item in a["tests"].values():
        assert item["familywise_max_t_tail"]>=item["uncorrected_bootstrap_tail"]
        assert 0<item["familywise_max_t_tail"]<=1
    assert diagnostic_summary(folds)["familywise_historical_sensitivity"]["eligible_for_automatic_promotion"] is False


def test_bootstrap_rejects_misalignment_missing_candidate_and_short_fold():
    base={"buy_hold_same_exposure":np.zeros(70),"a":np.ones(70)*.01}
    with pytest.raises(ValueError):
        exploratory_familywise_max_t([base],candidates=["other"])
    with pytest.raises(ValueError):
        exploratory_familywise_max_t([base,{"buy_hold_same_exposure":np.zeros(70),"a":np.zeros(69)}])
    with pytest.raises(ValueError):
        exploratory_familywise_max_t([{"buy_hold_same_exposure":np.zeros(3),"a":np.ones(3)}])
    with pytest.raises(ValueError):
        exploratory_familywise_max_t([base],candidates=["a","a"])


def test_zero_variance_equal_benchmark_cannot_generate_confident_winner():
    fold={"buy_hold_same_exposure":np.full(55,.001),
          "cash":np.full(55,.001)}
    result=exploratory_familywise_max_t([fold,fold],repetitions=150)
    assert result["tests"]["cash"]["estimable"] is False
    assert result["tests"]["cash"]["familywise_max_t_tail"]==1
    assert result["tests"]["cash"]["observed_studentized_statistic"] is None


@pytest.fixture
def frozen(tmp_path):
    db=tmp_path/"shadow.db"
    bars=demo_daily(320)
    bars["date"]=pd.date_range("2025-12-04",periods=320)
    # date range ends 2026-10-19 (320 days back from Oct 19)
    bars["date"]=pd.date_range(end="2026-10-19",periods=320)
    bootstrap(db,[ASSET],Settings(),now=START,fetch_fn=lambda _:bars)
    return db


def test_lock_prospective_future_only_and_no_interim_returns(frozen,tmp_path):
    target=tmp_path/"fixed.json"
    manifest=lock_protocol(frozen,target,now=START,days=90)
    assert manifest["first_eligible_decision_utc"]=="2026-10-21"
    assert manifest["required_complete_utc_days"]==90
    assert len(manifest["account_set"])==3
    assert manifest["blinded_holdout"] is False
    with pytest.raises(ProtocolError,match="Never overwrite"):
        lock_protocol(frozen,target,now=START,days=90)
    inspected=inspect_protocol(frozen,target,now=START)
    assert inspected["status"]=="LOCKED_UNTIL_FUTURE_CUTOFF"
    assert inspected["observed_strategy_returns"] is None
    assert not inspected["real_orders_enabled"]
    with pytest.raises(ProtocolError):
        lock_protocol(frozen,tmp_path/"bad.json",now=START,days=10)


def test_manifest_tampering_or_later_track_config_change_rejected(frozen,tmp_path):
    target=tmp_path/"fixed.json"
    lock_protocol(frozen,target,now=START)
    data=json.loads(target.read_text())
    data["required_complete_utc_days"]=30
    target.write_text(json.dumps(data))
    with pytest.raises(ProtocolError,match="hash changed"):
        read_protocol(target)
    target.unlink()
    lock_protocol(frozen,target,now=START)
    with sqlite3.connect(frozen) as con:
        con.execute("UPDATE shadow_accounts SET spec_json=? WHERE label='trend_ma20_80'",
                    ('{"label":"trend_ma20_80","kind":"momentum","fast":21,"slow":80}',))
    result=inspect_protocol(frozen,target,now=START)
    assert result["status"]=="INVALID_LEDGER"
    assert not result["eligible_for_promotion"]


def test_future_window_with_no_observations_is_incomplete_not_profitable(frozen,tmp_path):
    target=tmp_path/"fixed.json"
    d=lock_protocol(frozen,target,now=START,days=30)
    end=datetime.fromisoformat(d["evaluation_not_before_utc"])+timedelta(days=5)
    result=inspect_protocol(frozen,target,now=end)
    assert result["status"]=="INCOMPLETE_OR_HALTED"
    assert all(x["missing_days"]==30 and x["observed_net_return"] is None
               for x in result["strategies"])
    assert not result["confirmatory_inference_valid"]
    assert not result["eligible_for_promotion"]


def test_complete_simulated_future_30day_zero_return_never_authorizes_orders(frozen,tmp_path):
    """Offline harness inserts synthetic HOLD observations. No actual paper executions."""
    target=tmp_path/"fixed.json"
    manifest=lock_protocol(frozen,target,now=START,days=30)
    first=datetime.fromisoformat(manifest["first_eligible_decision_utc"]+"T00:00:00+00:00")
    with sqlite3.connect(frozen) as con:
        tracks=con.execute("SELECT track_id,initial_cash FROM shadow_accounts").fetchall()
        for track_id,initial in tracks:
            for i in range(30):
                d=first+timedelta(days=i)
                event=(track_id,d.date().isoformat(),(d+timedelta(days=1)).date().isoformat(),
                       "HOLD",0,100.0,0.,0.,initial,0.,initial,(d+timedelta(days=1)).isoformat())
                con.execute("INSERT INTO shadow_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",event)
            con.execute(
                "UPDATE shadow_accounts SET day_date=?,last_equity=? WHERE track_id=?",
                ((first+timedelta(days=30)).date().isoformat(),initial,track_id),
            )
    after=datetime.fromisoformat(manifest["evaluation_not_before_utc"])+timedelta(days=1)
    result=inspect_protocol(frozen,target,now=after)
    assert result["status"]=="OBSERVED_FOR_MANUAL_REVIEW"
    assert all(x["complete_observations"] and x["log_excess_vs_hold"]==0
               for x in result["strategies"])
    assert not result["confirmatory_inference_valid"]
    assert not result["real_orders_enabled"]

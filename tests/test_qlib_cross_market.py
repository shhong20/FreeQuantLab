"""Cross-market frozen data, causal label and no-session-filling protections."""
from __future__ import annotations

from hashlib import sha256
import json
import numpy as np
import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.qlib_cross_market import (
    GROUPS,validate_market,match_calendars,source_fit_target_predict,
    evaluate_transfer,evaluate_snapshot,
)


def fake_frame(n=850, *, crypto=True, scale=1.):
    frame=demo_daily(n)
    date=pd.date_range("2020-01-01",periods=n,freq="D" if crypto else "B")
    frame["date"]=date
    for col in ("open","high","low","close"):
        frame[col]*=scale
    return frame


def markets(n=850):
    return {
        "crypto_utc_24h":{
            "KRW-BTC":fake_frame(n),
            "KRW-XRP":fake_frame(n,scale=1.2),
            "KRW-ADA":fake_frame(n,scale=.9),
        },
        "krx_local_sessions":{
            "069500":fake_frame(n,crypto=False),
            "114800":fake_frame(n,crypto=False,scale=.8),
            "132030":fake_frame(n,crypto=False,scale=1.6),
        },
    }


def fake_predict(source,target,start,state_dir):
    assert source is not target
    p=np.full(len(target),.48,dtype=float)
    p[start:]=np.where(np.arange(len(target)-start)%2==0,.59,.41)
    return p,{"train_source_only":True,"target_labels_used_in_fit":False,
              "trained_source_max_label_open_index":start-1,
              "fit_end_exclusive":start}


def test_no_forward_filled_crypto_days_or_duplicate_markets():
    df=fake_frame()
    with pytest.raises(ValueError,match="Missing exchange"):
        validate_market(df.drop(index=20),"upbit")
    with pytest.raises(ValueError,match="reordered"):
        validate_market(df.iloc[::-1],"upbit")
    with pytest.raises(ValueError,match="duplicate"):
        validate_market(pd.concat([df.iloc[:40],df.iloc[39:]]),"upbit")


def test_krx_weekdays_and_holidays_not_manufactured():
    df=fake_frame(850,crypto=False)
    assert len(validate_market(df,"naver"))==850
    invalid=df.copy()
    invalid.at[25,"date"]=invalid.at[24,"date"]
    with pytest.raises(ValueError):
        validate_market(invalid,"naver")
    gap=df.drop(index=range(100,120))
    with pytest.raises(ValueError,match="long periods"):
        validate_market(gap,"naver")


def test_source_target_strict_identity_and_known_universe():
    data=markets()
    for group in GROUPS:
        source=data[group][GROUPS[group]["source"]]
        for target in GROUPS[group]["targets"]:
            match_calendars(validate_market(source,GROUPS[group]["feed"]),
                            validate_market(data[group][target],GROUPS[group]["feed"]))
    data["krx_local_sessions"]["132030"]=data["krx_local_sessions"]["132030"].drop(index=20)
    with pytest.raises(ValueError,match="not identical"):
        evaluate_transfer(data,state_dir="unused",predictor=fake_predict,max_folds=1)


def test_same_calendar_folds_frozen_candidates_costs_and_no_live_promotion():
    result=evaluate_transfer(markets(),state_dir="unused",predictor=fake_predict,max_folds=2)
    assert not result["risk_contract"]["real_orders_enabled"]
    assert not result["risk_contract"]["automatic_strategy_promotion"]
    assert result["risk_contract"]["target_labels_never_used_for_training"]
    assert set(result["markets"])==set(GROUPS)
    for group,cfg in GROUPS.items():
        values=result["markets"][group]
        assert len(values["targets"])==2
        for symbol,obj in values["targets"].items():
            assert obj["training_source"]==cfg["source"]
            assert len(obj["folds"])==2
            assert len(obj["score_rows"])==2*3*3
            for fold in obj["folds"]:
                proof=fold["source_only_training_proof"]
                assert proof["trained_source_max_label_open_index"]<proof["fit_end_exclusive"]
                assert proof["target_labels_used_in_fit"] is False
                assert fold["probabilities"]["observations"]==120
            for name in ("source_qlib_055","buy_hold_20pct","ma20_80_20pct"):
                rows={r["cost_bps"]:r for r in obj["summary"] if r["name"]==name}
                assert rows[15]["compounded_independent_fold_return"]>=rows[30]["compounded_independent_fold_return"]
                assert rows[30]["compounded_independent_fold_return"]>=rows[60]["compounded_independent_fold_return"]
            assert obj["paired_multiple_testing"]["eligible_for_automatic_promotion"] is False


def test_corrupted_snapshot_fails_without_any_model_training(tmp_path):
    root=tmp_path/"snap"
    root.mkdir()
    meta={"groups":{}}
    for name,cfg in GROUPS.items():
        meta["groups"][name]={"source":cfg["source"],"target_ids":list(cfg["targets"]),
                              "assets":{}}
        for sym in (cfg["source"],*cfg["targets"]):
            filename=f"{name}_{sym.replace('-','_')}.csv"
            meta["groups"][name]["assets"][sym]={
                "filename":filename,"sha256":"0"*64,"records":850,
                "first":"2020-01-01","last":"2022-01-01",
            }
            (root/filename).write_text("tampered")
    (root/"manifest.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError,match="SHA-256"):
        evaluate_snapshot(snapshot_dir=root,report_dir=tmp_path/"new",state_dir="unused")
    assert not (tmp_path/"new").exists()


def test_genuine_qlib_model_source_future_cannot_change_target_oos_predictions(tmp_path):
    pytest.importorskip("qlib")
    data=markets()
    source=data["crypto_utc_24h"]["KRW-BTC"]
    target=data["crypto_utc_24h"]["KRW-ADA"]
    first,proof=source_fit_target_predict(source,target,600,tmp_path/"qlib")
    edited=source.copy()
    edited.loc[600:,["open","high","low","close"]]*=1.25
    edited.loc[600:,"volume"]*=1.4
    second,proof2=source_fit_target_predict(edited,target,600,tmp_path/"qlib")
    np.testing.assert_allclose(first[600:720],second[600:720],atol=1e-12,rtol=0)
    assert proof["target_labels_used_in_fit"]==proof2["target_labels_used_in_fit"]==False
    assert proof["trained_source_max_label_open_index"]<600

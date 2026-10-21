"""Offline chronological stress regression with synthetic market data only."""
from __future__ import annotations

import sqlite3
import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.historical_stress import evaluate, verify_input, compare_frozen_archive


def synthetic(n=850):
    df=demo_daily(n)
    df["date"]=pd.date_range("2021-01-01",periods=n)
    return df


def test_nonoverlapping_windows_frozen_rules_and_no_automatic_promotion():
    result=evaluate(synthetic(),include_ml=False,max_t_reps=100)
    rows=result["folds"]
    assert result["status"]=="EXPLORATORY_NOT_INDEPENDENT_ALPHA_PROOF"
    assert result["real_orders_enabled"] is False
    assert result["automatic_strategy_promotion"] is False
    assert len(rows)>0
    assert len(rows)==len(result["summary"])*result["protocol"]["oos_folds"]
    reference=[r for r in rows if r["name"]=="buy_hold_20pct" and
               r["cost_bps_per_side"]==15]
    assert all(reference[i]["decision_last"] < reference[i+1]["decision_first"]
               for i in range(len(reference)-1))
    assert sum(r["oos_days"] for r in reference)<=len(synthetic())-602
    assert all(r["decision_first"]>r["training_closed_through"] for r in reference)
    assert result["multiple_testing_sensitivity"]["eligible_for_automatic_promotion"] is False


def test_gap_in_market_dates_is_rejected_not_synthesized():
    df=synthetic()
    with pytest.raises(ValueError,match="Missing calendar days"):
        verify_input(df.drop(index=200))


def test_closed_market_history_already_frozen_cannot_be_changed(tmp_path):
    df=synthetic()
    db=tmp_path/"history.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE archive_candles (symbol TEXT,utc_day TEXT,"
                    "open REAL,high REAL,low REAL,close REAL,volume REAL)")
        for r in df.iloc[-200:].itertuples():
            con.execute("INSERT INTO archive_candles VALUES(?,?,?,?,?,?,?)",
                        ("KRW-BTC",r.date.strftime("%Y-%m-%d"),r.open,r.high,
                         r.low,r.close,r.volume))
    assert compare_frozen_archive(df,"KRW-BTC",db)["compared_days"]==200
    altered=df.copy()
    altered.loc[850-100,"close"]*=1.2
    with pytest.raises(ValueError,match="changed"):
        compare_frozen_archive(altered,"KRW-BTC",db)


def test_replay_rejects_snapshot_sha256_tamper_without_network(tmp_path):
    from quantlab.historical_stress import run_snapshot
    import json
    source=tmp_path/"source"
    source.mkdir()
    (source/"KRW_BTC.csv").write_text("changed")
    (source/"results.json").write_text(json.dumps({
        "datasets":{"KRW-BTC":{"filename":"KRW_BTC.csv","sha256":"0"*64}}
    }))
    with pytest.raises(ValueError,match="SHA-256"):
        run_snapshot(tmp_path/"fresh",source,tmp_path/"archive.db")
    assert not (tmp_path/"fresh").exists()


def test_duplicate_or_reordered_source_is_never_silently_fixed():
    df=synthetic()
    repeated=pd.concat((df.iloc[:250],df.iloc[249:]),ignore_index=True)
    with pytest.raises(ValueError,match="Duplicate"):
        verify_input(repeated)
    reordered=df.iloc[::-1].reset_index(drop=True)
    with pytest.raises(ValueError,match="reordered"):
        verify_input(reordered)

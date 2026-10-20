"""Strict feed integrity and forensic paper-ledger read-only audit tests."""
from datetime import datetime,timezone
import math
import sqlite3

import pandas as pd
import pytest

from quantlab.data import demo_daily
from quantlab.quality import (
    MarketIntegrityError, validate_shadow_market, validate_public_quote,
    audit_dataset_snapshot,
)
from quantlab.ledger_audit import audit_shadow_ledger, evaluate_manual_review
from quantlab.forward_reports import report
from quantlab.shadow import bootstrap, run_cycle
from quantlab.settings import Settings


NOW=datetime(2026,10,21,0,18,tzinfo=timezone.utc)
PREV=datetime(2026,10,20,0,18,tzinfo=timezone.utc)
ASSET={"provider":"upbit","symbol":"KRW-BTC","kind":"crypto",
       "currency":"KRW","bars":320}


def bars(last="2026-10-20"):
    d=demo_daily(320)
    d["date"]=pd.date_range(end=last,periods=len(d))
    return d


def test_strict_shadow_quality_valid_complete_history():
    info=validate_shadow_market(bars(),NOW)
    assert info["status"]=="PASS"
    assert info["completed_utc_day"]=="2026-10-20"
    assert info["inspected_days"]==100


@pytest.mark.parametrize("mutation",[
    lambda d: d.__setitem__("date",d["date"].where(d.index!=318,d["date"].iloc[317])),
    lambda d: d.__setitem__("date",d["date"].where(d.index!=319,pd.Timestamp("2026-10-22"))),
    lambda d: d.__setitem__("high",d["high"].where(d.index!=319,d["close"].iloc[319]*.1)),
    lambda d: d.__setitem__("volume",d["volume"].where(d.index!=319,0.)),
    lambda d: d.__setitem__("close",d["close"].where(d.index!=319,float("nan"))),
    lambda d: d.__setitem__("close",d["close"].where(d.index!=319,d["close"].iloc[318]*3.)),
])
def test_shadow_data_quality_rejects_corrupt_ohlcv(mutation):
    d=bars()
    mutation(d)
    with pytest.raises(MarketIntegrityError):
        validate_shadow_market(d,NOW)


def test_reject_quote_disconnected_from_last_close():
    with pytest.raises(MarketIntegrityError):
        validate_public_quote(100.,200.)
    with pytest.raises(MarketIntegrityError):
        validate_public_quote(100.,float("nan"))
    validate_public_quote(100.,100.)


def test_research_ohlcv_allows_weekend_gaps_but_rejects_duplicates():
    df=demo_daily(300)
    result=audit_dataset_snapshot(df.iloc[::3].copy())
    assert result["status"]=="PASS"
    bad=df.iloc[:50].copy()
    bad.loc[49,"date"]=bad.loc[48,"date"]
    with pytest.raises(MarketIntegrityError):
        audit_dataset_snapshot(bad)


def test_shadow_bootstrap_and_live_cycle_fail_closed_on_bad_prices(tmp_path):
    db=tmp_path/"shadow.db"
    bootstrap(db,[ASSET],Settings(),now=PREV,
              fetch_fn=lambda _:bars("2026-10-19"))
    extreme=run_cycle(
        db,[ASSET],now=NOW,fetch_fn=lambda _:bars(),
        quote_fn=lambda p,s,*,now:(1_000_000.,now.date().isoformat()),
    )
    assert extreme["status"]=="ERROR"
    assert extreme["errors"]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]==0


def test_readonly_ledger_replays_valid_forward_virtual_fills(tmp_path):
    db=tmp_path/"shadow.db"
    bootstrap(db,[ASSET],Settings(),now=PREV,
              fetch_fn=lambda _:bars("2026-10-19"))
    initial=audit_shadow_ledger(db,now=PREV)
    assert initial["status"]=="PASS" and initial["events"]==0
    res=run_cycle(db,[ASSET],now=NOW,fetch_fn=lambda _:bars(),
        quote_fn=lambda p,s,*,now:(float(bars()["close"].iloc[-1]),now.date().isoformat()),
    )
    assert res["status"]=="OK"
    audit=audit_shadow_ledger(db,now=NOW)
    assert audit["status"]=="PASS",audit["issues"]
    assert audit["accounts"]==3 and audit["events"]==3
    forward=report(db,now=NOW)
    evals=evaluate_manual_review(forward,audit)
    assert evals["any_broker_order_permission"] is False
    assert evals["strategy_auto_promotion"] is False
    assert all(x["research_maturity"]=="NOT_READY" for x in evals["strategies"])
    assert all("at_least_90_or_configured_forward_days" in x["missing_checks"]
               for x in evals["strategies"])


def test_ledger_detects_account_balance_tamper_without_repair(tmp_path):
    db=tmp_path/"shadow.db"
    bootstrap(db,[ASSET],Settings(),now=PREV,
              fetch_fn=lambda _:bars("2026-10-19"))
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shadow_accounts SET cash=cash+777")
    before=db.stat().st_mtime_ns
    quality=audit_shadow_ledger(db)
    assert quality["status"]=="FAIL"
    assert any(x["code"]=="ACCOUNT_STATE_DIFFERS_FROM_REPLAY" for x in quality["issues"])
    assert quality["repair_performed"] is False
    assert db.stat().st_mtime_ns==before


def test_ledger_detects_bad_frozen_strategy_identity(tmp_path):
    db=tmp_path/"shadow.db"
    bootstrap(db,[ASSET],Settings(),now=PREV,
              fetch_fn=lambda _:bars("2026-10-19"))
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shadow_accounts SET spec_json=? WHERE label='trend_ma20_80'",
                    ('{"label":"trend_ma20_80","kind":"momentum","fast":30,"slow":90}',))
    audit=audit_shadow_ledger(db)
    assert audit["status"]=="FAIL"
    assert "FROZEN_STRATEGY_IDENTITY_CHANGED" in {x["code"] for x in audit["issues"]}


def test_ledger_tampered_event_fee_is_detected(tmp_path):
    db=tmp_path/"shadow.db"
    bootstrap(db,[ASSET],Settings(),now=PREV,
              fetch_fn=lambda _:bars("2026-10-19"))
    run_cycle(db,[ASSET],now=NOW,fetch_fn=lambda _:bars(),
              quote_fn=lambda p,s,*,now:(float(bars()["close"].iloc[-1]),
                                         now.date().isoformat()))
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shadow_events SET simulated_cost=simulated_cost+11 WHERE action='BUY'")
    audit=audit_shadow_ledger(db)
    assert audit["status"]=="FAIL"
    assert "INCORRECT_BUY_FEE_OR_VOLUME" in {x["code"] for x in audit["issues"]}


def test_negative_ledger_prohibits_research_ready_even_if_thresholds_relaxed():
    report_data={"strategies":[{
        "track_id":"t1","symbol":"KRW-BTC","strategy":"bad",
        "recorded_days":100,"missing_intermediate_utc_days":0,
        "simulated_orders":20,"halted":False,
        "observed_max_drawdown":-.02,
        "comparison_observations_aligned":True,
        "vs_buy_hold_return_pp":10.,
    }]}
    corrupted={"status":"FAIL","tracks":[{"track_id":"t1","status":"FAIL"}]}
    out=evaluate_manual_review(report_data,corrupted)
    assert out["strategies"][0]["research_maturity"]=="NOT_READY"
    assert not out["strategies"][0]["checks"]["ledger_reconciles"]


def test_watchdog_quarantines_corrupt_ledger_and_emits_persistent_alert(tmp_path):
    from quantlab.watchdog import check
    ledger=tmp_path/"shadow.db"
    bootstrap(ledger,[ASSET],Settings(),now=PREV,
              fetch_fn=lambda _:bars("2026-10-19"))
    with sqlite3.connect(ledger) as con:
        con.execute("UPDATE shadow_accounts SET last_equity=last_equity+100")
    result=check(shadow_db=ledger,state_db=tmp_path/"watchdog.db",
                 report_dir=tmp_path/"reports",now=NOW,
                 windows_notifications=False,health_check=lambda:True)
    assert result["status"]=="DEGRADED"
    assert not result["reports"]
    assert any(x["code"]=="SHADOW_LEDGER_INTEGRITY_FAILED"
               for x in result["alert_transitions"])
    assert not list((tmp_path/"reports").glob("*.json"))
    assert not result["real_orders_enabled"]


def test_watchdog_report_contains_conservative_manual_review(tmp_path):
    from quantlab.watchdog import check
    import json
    ledger=tmp_path/"shadow.db"
    bootstrap(ledger,[ASSET],Settings(),now=PREV,
              fetch_fn=lambda _:bars("2026-10-19"))
    outcome=check(shadow_db=ledger,state_db=tmp_path/"watchdog.db",
                  report_dir=tmp_path/"reports",now=PREV,
                  windows_notifications=False,health_check=lambda:True)
    assert outcome["status"]=="OK"
    body=json.loads((tmp_path/"reports"/"2026-10-20.json").read_text())
    assert body["ledger_integrity"]["status"]=="PASS"
    assert body["manual_review"]["strategy_auto_promotion"] is False
    assert all(x["research_maturity"]=="NOT_READY"
               for x in body["manual_review"]["strategies"])


def test_quote_outlier_blocked_for_both_assets_even_if_account_would_hold(tmp_path):
    ledger=tmp_path/"shadow.db"
    bootstrap(ledger,[ASSET],Settings(),now=PREV,
              fetch_fn=lambda _:bars("2026-10-19"))
    outcome=run_cycle(ledger,[ASSET],now=NOW,fetch_fn=lambda _:bars(),
                      quote_fn=lambda p,s,*,now:(.01,now.date().isoformat()))
    assert outcome["status"]=="ERROR"
    assert "MarketIntegrityError" in outcome["errors"][0]["error"]
    with sqlite3.connect(ledger) as con:
        assert con.execute("SELECT COUNT(*) FROM shadow_events").fetchone()[0]==0


def test_upbit_raw_duplicate_sessions_do_not_get_silently_normalized(monkeypatch):
    from quantlab import data
    from quantlab.data import DataError, upbit_daily
    class FakeResponse:
        def json(self):
            return [{
                "candle_date_time_utc":"2026-10-19T00:00:00",
                "opening_price":100, "high_price":101, "low_price":99,
                "trade_price":100, "candle_acc_trade_volume":100,
            }] * 2
    monkeypatch.setattr(data,"_get",lambda url,**kwargs: FakeResponse())
    with pytest.raises(DataError,match="duplicate UTC"):
        upbit_daily("KRW-BTC",bars=80,now=NOW)


def test_qlib_research_frame_disjoint_and_rejects_duplicate_sessions(monkeypatch,tmp_path):
    from quantlab.qlib_bridge import qlib_walk_forward
    def mock_probability(df,fit_end,state_dir,threshold=.55):
        import numpy as np
        return np.zeros(len(df))
    monkeypatch.setattr("quantlab.qlib_bridge.qlib_signals",mock_probability)
    full=demo_daily(520)
    quality=qlib_walk_forward(full,Settings(),tmp_path)
    assert quality["data_quality"]["status"]=="PASS"
    corrupt=full.copy()
    corrupt.loc[400,"date"]=corrupt.loc[399,"date"]
    with pytest.raises(MarketIntegrityError):
        qlib_walk_forward(corrupt,Settings(),tmp_path)

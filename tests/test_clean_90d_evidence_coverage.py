"""Clean forward 90-day evidence coverage is independent and never backfills."""
from __future__ import annotations

from datetime import datetime,timezone
from hashlib import sha256
import json
import sqlite3

import pytest

from quantlab import clean_90d_evidence_coverage as m
from quantlab.shadow import run_cycle
from test_clean_cohort import ASSETS,bars,cohort,record


def _observe(db,protocol,dec,when):
    frame=bars(dec)
    price=float(frame["close"].iloc[-1])
    return run_cycle(db,ASSETS,now=when,
        fetch_fn=lambda _:frame.copy(),
        quote_fn=lambda p,s,*,now:(price,now.date().isoformat()),
        protocol_path=protocol)


def test_missing_day_with_late_run_receipt_and_two_true_valid_days(cohort,tmp_path):
    db,protocol=cohort
    t0=datetime(2026,10,23,0,24,tzinfo=timezone.utc)
    assert record(db,protocol,now=t0,day="2026-10-22")["status"]=="OK"
    # The next day is missed; the process starts after the allowable window.
    late=datetime(2026,10,24,4,31,tzinfo=timezone.utc)
    result=run_cycle(db,ASSETS,now=late,
        fetch_fn=lambda _:pytest.fail("late run must not synthesize historical bars"),
        quote_fn=lambda *a,**kw:pytest.fail("late run must not request ticker"),
        protocol_path=protocol)
    assert result["results"][0]["status"]=="OUTSIDE_OBSERVATION_WINDOW"
    assert _observe(db,protocol,"2026-10-24",
                    datetime(2026,10,25,0,24,tzinfo=timezone.utc))["status"]=="OK"
    clock=datetime(2026,10,25,1,21,tzinfo=timezone.utc)
    report=m.build(db=db,protocol=protocol,now=clock)
    assert report["status"]==m.STATUS_MISSING
    assert report["expected_account_day_observations"]==18
    assert report["valid_account_day_observations"]==12
    assert report["missing_account_day_observations"]==6
    assert report["invalid_account_day_observations"]==0
    assert report["complete_decision_days"]==2
    assert report["all_missing_decision_days"]==1
    assert report["days_with_late_window_run_and_no_decision"]==1
    assert report["first_missing_decision_date"]=="2026-10-23"
    assert report["strategy_returns"] is None
    assert report["strategy_sharpe"] is None
    assert report["research_eligible_for_promotion"] is False
    assert report["broker_orders"]==0
    r=report["by_decision_utc_day"][1]
    assert r["state"]=="ALL_MISSING"
    assert r["gap_receipt_classification"]=="AFTER_WINDOW_RUN_RECEIPT_WITH_NO_DECISION"
    assert r["run_evidence"]["count"]==1
    assert r["run_evidence"]["after_window_count"]==1
    assert r["run_evidence"]["receipts"][0]["top_level_status"]=="OK"
    assert "OUTSIDE_OBSERVATION_WINDOW" in r["run_evidence"]["receipts"][0]["result_statuses"]
    assert all(item["valid_observed_days"]==2 for item in report["strategy_observation_quality"])
    dest=tmp_path/"report"
    created=m.capture(db=db,protocol=protocol,output_dir=dest,now=clock)
    assert created==report
    assert m.verify(db=db,protocol=protocol,output_dir=dest)==report
    assert dest.stat().st_mode&0o777==0o700
    assert all(x.stat().st_mode&0o777==0o600 for x in dest.iterdir())
    with pytest.raises(ValueError,match="never overwrite"):
        m.capture(db=db,protocol=protocol,output_dir=dest,now=clock)
    assert m._digest(m._select(db,clock))==report["source_row_snapshot_sha256"]


def test_fully_valid_first_day_is_coverage_not_strategy_return(cohort):
    db,protocol=cohort
    record(db,protocol)
    report=m.build(db=db,protocol=protocol,
                   now=datetime(2026,10,23,1,21,tzinfo=timezone.utc))
    assert report["status"]==m.STATUS_COMPLETE
    assert report["expected_account_day_observations"]==6
    assert report["valid_account_day_observations"]==6
    assert report["missing_account_day_observations"]==0
    assert report["broker_orders"]==0
    assert report["strategy_returns"] is None
    assert report["research_eligible_for_promotion"] is False


def test_missed_run_receipt_not_mislabeled_as_server_shutdown(cohort):
    db,protocol=cohort
    now=datetime(2026,10,23,1,21,tzinfo=timezone.utc)
    report=m.build(db=db,protocol=protocol,now=now)
    assert report["expected_account_day_observations"]==6
    assert report["missing_account_day_observations"]==6
    assert report["by_decision_utc_day"][0][
        "gap_receipt_classification"]=="NO_RUN_RECEIPT_FOR_EXPECTED_SESSION"
    assert report["missing_reason_is_receipt_evidence_not_confirmed_power_outage"]


def test_deleted_or_changed_quote_is_rejected_by_quality_guard(cohort):
    db,protocol=cohort
    record(db,protocol)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shadow_quote_evidence SET public_ticker_price=1 "
                    "WHERE symbol='KRW-ETH'")
    with pytest.raises(ValueError,match="Independent ledger/quote audit did not pass"):
        m.build(db=db,protocol=protocol,
                now=datetime(2026,10,23,1,21,tzinfo=timezone.utc))


def test_forged_approval_and_rehashed_report_fails_independent_reconstruction(cohort,tmp_path):
    db,protocol=cohort
    record(db,protocol)
    dest=tmp_path/"r"
    m.capture(db=db,protocol=protocol,output_dir=dest,
              now=datetime(2026,10,23,1,21,tzinfo=timezone.utc))
    path=dest/"report.json"
    data=json.loads(path.read_text())
    data["paper_or_live_trading_approved"]=True
    body=m._jsonbytes(data)
    path.write_bytes(body)
    (dest/"report.sha256").write_text(sha256(body).hexdigest()+"\n")
    with pytest.raises(ValueError,match="disagrees"):
        m.verify(db=db,protocol=protocol,output_dir=dest)


def test_prospective_next_day_does_not_fill_old_immutable_report(cohort,tmp_path):
    db,protocol=cohort
    record(db,protocol)
    now=datetime(2026,10,23,1,21,tzinfo=timezone.utc)
    dest=tmp_path/"r"
    original=m.capture(db=db,protocol=protocol,output_dir=dest,now=now)
    assert _observe(db,protocol,"2026-10-23",
                    datetime(2026,10,24,0,24,tzinfo=timezone.utc))["status"]=="OK"
    # New prospective activity must not be retroactively included in old report.
    assert m.verify(db=db,protocol=protocol,output_dir=dest)==original


def test_action_semantics_not_confused_with_signal_predictive_accuracy():
    now=datetime(2026,10,23,0,24,tzinfo=timezone.utc)
    event={"decision_date":"2026-10-22","session_date":"2026-10-23",
           "created_at":now.isoformat(),"action":"BUY","target":1,
           "observed_price":101.0}
    quote={"decision_date":"2026-10-22","session_date":"2026-10-23",
           "observed_at_utc":now.isoformat(),"source":"upbit_public_ticker",
           "public_ticker_price":101.0}
    assert m._validated(event,quote,now)
    assert not m._validated({**event,"target":0},quote,now)
    assert not m._validated(event,{**quote,"public_ticker_price":102.},now)
    assert not m._validated({**event,"created_at":"2026-10-23T03:24:00+00:00"},quote,
                            datetime(2026,10,23,5,0,tzinfo=timezone.utc))

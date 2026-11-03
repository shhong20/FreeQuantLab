"""Postopen evidence chain: same-day result separate from old missing 90d days."""
from __future__ import annotations

from datetime import date,datetime,timezone,timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from quantlab import clean_postopen_chain as chain
from quantlab.clean_preopen_readiness import _save_receipt as preopen_save
from quantlab.shadow import run_cycle
from test_clean_cohort import ASSETS,cohort,bars,record


UTC=timezone.utc
FIRST_DAY=date(2026,10,23)
OUT=datetime(2026,10,23,1,56,tzinfo=UTC)


def _preopen(folder:Path,when:datetime,*,ready=True):
    time=when.astimezone(chain.KST)
    body={
        "schema":chain.PREOPEN_SCHEMA,
        "checked_at_utc":when.astimezone(UTC).isoformat(),
        "kst_date":time.date().isoformat(),
        "kst_clock":time.strftime("%H:%M"),
        "status":"READY_FOR_FUTURE_OBSERVATION_ONLY" if ready
                 else "INFRA_READINESS_FAILED_NO_AUTOMATIC_ORDERS",
        "infrastructure_ready":ready,
        "calendar_timers_ready":ready,
        "public_market_reachable":ready,
        "observation_window_status":"PREOPEN" if time.hour<9
                                     else "OBSERVATION_WINDOW",
        "frozen_accounts_verified":6,
        "live_orders":0,
        "past_virtual_orders_created":0,
        "future_09_24_observation_guaranteed":False,
        "real_trading_enabled":False,
        "strategy_promotion_enabled":False,
    }
    return preopen_save(body,folder)


def _all_three(folder):
    first=datetime(2026,10,22,23,46,tzinfo=UTC)
    _preopen(folder,first)
    _preopen(folder,datetime(2026,10,23,0,7,tzinfo=UTC))
    _preopen(folder,datetime(2026,10,23,0,12,tzinfo=UTC))


def _report(cohort,tmp_path,*,now=OUT,rollout=FIRST_DAY):
    db,protocol=cohort
    return chain.assess(db=db,protocol=protocol,
                        preopen_dir=tmp_path/"ready",now=now,
                        rollout=rollout)


def test_today_all_six_real_records_old_gaps_not_a_return(cohort,tmp_path):
    db,protocol=cohort
    assert record(db,protocol)["status"]=="OK"
    _all_three(tmp_path/"ready")
    result=_report(cohort,tmp_path)
    assert result["status"]=="DAY_COMPLETE_WITH_PREOPEN_EVIDENCE"
    assert result["today_decision_status"]=="SIX_OF_SIX_VALID_ORIGINAL_FORWARD_DECISIONS"
    assert result["today_valid"]==6
    assert result["today_missing"]==0
    assert result["today_invalid"]==0
    assert result["today_in_window_run_count"]==1
    assert result["preopen_status"]=="PREOPEN_ALL_THREE_VERIFIED"
    assert all(result["preopen_evidence"]["slots"].values())
    assert result["cumulative_valid"]==6
    assert result["cumulative_missing"]==0
    assert not result["win32_login_resume_tasks_independently_attested_from_this_report"]
    assert result["broker_orders"]==0
    assert result["strategy_returns"] is None
    assert result["strategy_sharpe"] is None
    assert not result["day_completion_approved_as_strategy_performance"]
    assert not result["model_training_or_live_trading_approved"]
    assert not result["crypto_market_prices_written"]


def test_today_complete_can_coexist_with_past_six_missing(cohort,tmp_path):
    db,protocol=cohort
    # Miss initial decision Oct 22, record ONLY the NEXT valid decision Oct 23.
    later=datetime(2026,10,24,0,24,tzinfo=UTC)
    data=bars("2026-10-23")
    assert run_cycle(db,ASSETS,now=later,
           fetch_fn=lambda _:data.copy(),
           quote_fn=lambda p,s,*,now:(float(data["close"].iloc[-1]),
                                      now.date().isoformat()),
           protocol_path=protocol)["status"]=="OK"
    result=_report(cohort,tmp_path,now=datetime(2026,10,24,2,1,tzinfo=UTC),
                   rollout=date(2026,10,25))
    assert result["today_valid"]==6
    assert result["today_missing"]==0
    assert result["cumulative_missing"]==6
    assert result["cumulative_valid"]==6
    assert result["status"]=="DAY_COMPLETE_PREOPEN_NOT_YET_REQUIRED"
    assert result["cumulative_cohort_status"].startswith("INCOMPLETE")
    assert result["new_paper_fills_or_historical_replay"]==0


def test_missing_today_is_separately_failed_even_if_preopen_ready(cohort,tmp_path):
    _all_three(tmp_path/"ready")
    r=_report(cohort,tmp_path)
    assert r["status"]=="TODAY_DECISION_INCOMPLETE_NO_BACKFILL"
    assert r["today_valid"]==0
    assert r["today_missing"]==6
    assert r["today_decision_status"]=="SIX_MISSING_NO_RETROACTIVE_REPLAY"
    assert r["preopen_status"]=="PREOPEN_ALL_THREE_VERIFIED"
    assert r["broker_orders"]==0
    assert r["new_paper_fills_or_historical_replay"]==0


def test_late_run_receipt_is_not_a_valid_six_decisions(cohort,tmp_path):
    db,protocol=cohort
    late=datetime(2026,10,23,3,11,tzinfo=UTC)
    r=run_cycle(db,ASSETS,now=late,
                fetch_fn=lambda _:pytest.fail("No time travel candle"),
                quote_fn=lambda *a,**k:pytest.fail("No fabricated quote"),
                protocol_path=protocol)
    assert r["results"][0]["status"]=="OUTSIDE_OBSERVATION_WINDOW"
    result=_report(cohort,tmp_path,now=datetime(2026,10,23,3,55,tzinfo=UTC))
    assert result["today_missing"]==6
    assert result["today_late_run_count"]>=1
    assert result["today_valid"]==0
    assert not result["today_all_six_valid"]


def test_missing_preopen_is_visible_even_if_todays_decision_is_good(cohort,tmp_path):
    db,protocol=cohort
    record(db,protocol)
    result=_report(cohort,tmp_path)
    assert result["today_all_six_valid"]
    assert result["status"]=="DAY_COMPLETE_PREOPEN_PARTIAL_OR_UNOBSERVED"
    assert result["preopen_status"]=="NO_WSL_PREOPEN_RECEIPTS"
    assert not result["preopen_evidence"]["windows_warm_task_run_proven"]


def test_partial_readiness_does_not_get_full_three_slots(cohort,tmp_path):
    db,protocol=cohort
    record(db,protocol)
    _preopen(tmp_path/"ready",datetime(2026,10,23,0,7,tzinfo=UTC))
    result=_report(cohort,tmp_path)
    assert result["preopen_status"]=="PREOPEN_PARTIAL_VERIFIED"
    assert result["preopen_evidence"]["slots"]=={
        "08:46":False,"09:07":True,"09:12":False}
    assert result["today_all_six_valid"]
    assert result["status"]=="DAY_COMPLETE_PREOPEN_PARTIAL_OR_UNOBSERVED"


def test_failed_preopen_ticker_probe_is_not_greenwashed_by_time(cohort,tmp_path):
    db,protocol=cohort
    record(db,protocol)
    _preopen(tmp_path/"ready",datetime(2026,10,22,23,46,tzinfo=UTC),
             ready=False)
    r=_report(cohort,tmp_path)
    assert r["preopen_status"]=="NO_VERIFIED_PREOPEN_WINDOW"
    assert r["preopen_evidence"]["receipts_seen"]==1
    assert r["status"]=="DAY_COMPLETE_PREOPEN_PARTIAL_OR_UNOBSERVED"


def test_preopen_receipt_tampering_and_hash_rewrite_do_not_certify_approval(cohort,tmp_path):
    db,protocol=cohort
    record(db,protocol)
    path=_preopen(tmp_path/"ready",datetime(2026,10,22,23,46,tzinfo=UTC))
    raw=json.loads(path.read_bytes())
    raw["live_orders"]=1
    data=(json.dumps(raw,sort_keys=True,ensure_ascii=False)+"\n").encode()
    path.write_bytes(data)
    path.with_suffix(".json.sha256").write_text(sha256(data).hexdigest()+"\n")
    with pytest.raises(ValueError,match="contradicts"):
        _report(cohort,tmp_path)


def test_preopen_unpinned_bit_flip_fails_closed(cohort,tmp_path):
    record(*cohort)
    path=_preopen(tmp_path/"ready",datetime(2026,10,22,23,46,tzinfo=UTC))
    path.write_bytes(path.read_bytes()+b"tampering")
    with pytest.raises(ValueError,match="SHA"):
        _report(cohort,tmp_path)


def test_outside_observation_time_does_not_falsely_say_missing(cohort,tmp_path):
    # While the 90d observation window is still open, no due-day failure.
    result=_report(cohort,tmp_path,
                   now=datetime(2026,10,23,0,15,tzinfo=UTC))
    assert result["today_decision_status"]=="DUE_WINDOW_NOT_CLOSED_DO_NOT_IMPUTE"
    assert result["status"]=="WINDOW_NOT_CLOSED_DO_NOT_ASSESS_FILLS"


def test_immutable_signed_postopen_receipt_reconstructed(cohort,tmp_path):
    db,protocol=cohort
    record(db,protocol)
    _all_three(tmp_path/"ready")
    report=_report(cohort,tmp_path)
    path=chain._write_receipt(report,tmp_path/"out")
    assert chain.verify(receipt=path,db=db,protocol=protocol,
                        preopen_dir=tmp_path/"ready")==report
    assert path.parent.stat().st_mode&0o777==0o700
    assert path.stat().st_mode&0o777==0o600
    with pytest.raises(FileExistsError):
        chain._write_receipt(report,tmp_path/"out")
    # Misleading claimed strategy returns are rejected even if attacker rehashes.
    fake={**report,"strategy_returns":{"alpha":42}}
    body=chain._json_bytes(fake)
    path.write_bytes(body)
    path.with_suffix(".json.sha256").write_text(sha256(body).hexdigest()+"\n")
    with pytest.raises(ValueError,match="changed from independent"):
        chain.verify(receipt=path,db=db,protocol=protocol,
                     preopen_dir=tmp_path/"ready")


def test_postopen_report_is_stable_after_another_prospective_session(cohort,tmp_path):
    db,protocol=cohort
    record(db,protocol)
    _all_three(tmp_path/"ready")
    now=datetime(2026,10,23,1,56,tzinfo=UTC)
    report=_report(cohort,tmp_path,now=now)
    path=chain._write_receipt(report,tmp_path/"reports")
    new_time=datetime(2026,10,24,0,24,tzinfo=UTC)
    frame=bars("2026-10-23")
    run_cycle(db,ASSETS,now=new_time,fetch_fn=lambda _:frame.copy(),
              quote_fn=lambda p,s,*,now:(float(frame["close"].iloc[-1]),
                                         now.date().isoformat()),
              protocol_path=protocol)
    assert chain.verify(receipt=path,db=db,protocol=protocol,
                        preopen_dir=tmp_path/"ready")==report


def test_timer_is_after_existing_watchdog_and_never_runs_shadow_cycle():
    base=Path(__file__).resolve().parents[1]/"deploy/systemd"
    service=(base/"freequant-clean-postopen.service").read_text()
    timer=(base/"freequant-clean-postopen.timer").read_text()
    assert "After=freequant-clean-watchdog.service" in service
    assert "quantlab.clean_postopen_chain" in service
    assert "ReadWritePaths=/home/hong/apps/FreeQuantLab/state/postopen_chain" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=read-only" in service
    assert "OnCalendar=*-*-* 10:55:00 Asia/Seoul" in timer
    assert "OnCalendar=*-*-* 12:55:00 Asia/Seoul" in timer
    assert "OnStartupSec=13min" in timer
    assert "Persistent=true" in timer
    for prohibited in ("shadow-cycle","--capture","--backfill","broker-order"):
        assert prohibited not in service

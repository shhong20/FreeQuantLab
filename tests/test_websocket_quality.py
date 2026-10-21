"""Rolling sampled-WS quality and storage budget gates on independent SQLite."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from quantlab.continuous_portfolio import ASSETS
from quantlab.websocket_microstructure import (
    PUBLIC_WSS,open_db,validate_frame,append_validated,audit,
)
from quantlab.websocket_quality import evaluate_quality,main,WINDOWS_PER_DAY
from test_websocket_microstructure import book,trade


START=1792565400000 // 600000 * 600000
INTERVAL=600000


def populate(path,*,omit=frozenset(),count=WINDOWS_PER_DAY+1,
             empty_ada=True,bad_clock=False,failed_slot=None):
    conn=open_db(path)
    for index in range(count):
        if index in omit:
            continue
        slot_start=START+index*INTERVAL
        started=slot_start+120000
        session=f"s-{index}"
        with conn:
            conn.execute("""INSERT INTO sessions
                (id,started_ms,finished_ms,status,intended_seconds,endpoint,mode,
                 frame_count,rejected_frames,duplicated_trade_ids,reconnects)
                 VALUES(?,?,?,'RUNNING',30,?,'PUBLIC_READ_ONLY',0,0,0,0)""",
                 (session,started,started+30_000,PUBLIC_WSS))
            counts={"frames":0,"books":0,"trades":0,"duplicates":0,
                    "causal_trade_links":0,"unlinked_trades":0}
            prior={}
            for k,market in enumerate(ASSETS):
                book_ts=started+1000+k*20
                raw=json.dumps(book(market,book_ts)).encode()
                bx=validate_frame(raw,book_ts+130)
                if bad_clock and market=="KRW-BTC":
                    bx["receipt_ms"]+=5000
                append_validated(conn,session,bx,prior,counts)
                if market!="KRW-ADA" or not empty_ada:
                    tr=json.dumps(trade(market,book_ts+220,id=1000000+index*10+k)).encode()
                    tx=validate_frame(tr,book_ts+320)
                    append_validated(conn,session,tx,prior,counts)
            conn.execute("UPDATE sessions SET frame_count=?,status=?,reconnects=? WHERE id=?",
                         (counts["frames"],
                          "CAPTURE_DEGRADED_GAPS_OR_BAD_MESSAGES" if index==failed_slot else
                          "BOUNDED_CAPTURE_COMPLETE",
                          1 if index==failed_slot else 0,session))
    conn.close()


def at(idx):
    return START+idx*INTERVAL+120000+91000


def test_before_24h_small_sample_not_promoted_and_empty_market_trades_honest(tmp_path):
    p=tmp_path/"observe.sqlite"
    populate(p,count=3)
    report=evaluate_quality(p,now_ms=at(2))
    assert report["status"]=="COLLECTING_NOT_YET_24_HOURS"
    assert not report["provisional_24h_sample_gate_passed"]
    assert report["rolling_24h_successful_slot_count"]==3
    assert report["operational_missing_due_slots"]==0
    assert report["books_per_market_and_receipt_lag"]["KRW-ADA"]["market_trade_prints"]==0
    assert report["books_per_market_and_receipt_lag"]["KRW-ADA"]["limited_market_print_sample"]
    assert report["raw_cryptographic_audit_is_separate"]
    assert not report["model_calibration_allowed"]


def test_true_24h_good_schedule_passes_only_public_window_gate(tmp_path):
    p=tmp_path/"observe.sqlite"
    populate(p)
    report=evaluate_quality(p,now_ms=at(WINDOWS_PER_DAY))
    assert report["archive_age_hours"]>=24
    assert report["rolling_24h_successful_slot_count"]==WINDOWS_PER_DAY
    assert report["rolling_24h_fraction"]==pytest.approx(1)
    assert report["first_24h_available"]
    assert report["status"]=="PROSPECTIVE_WINDOW_OBSERVATION_PASS_NOT_EXECUTION"
    assert report["provisional_24h_sample_gate_passed"]
    assert report["rolling_24h_missing_due_slots"]==0
    assert all(x["unique_book_exchange_timestamps"]>=100
               for x in report["books_per_market_and_receipt_lag"].values())
    assert report["trade_prints_not_customer_fills"]
    assert not report["automatic_model_promotion"]
    assert report["wall_clock_observation_duty_fraction_at_full_schedule"]==.05
    assert audit(p,lookback_hours=None)["still_no_verified_own_fills"]


def test_extended_outage_is_degraded_and_no_retroactive_gap_fill(tmp_path):
    p=tmp_path/"observe.sqlite"
    omitted=set(range(60,81))
    populate(p,omit=omitted)
    report=evaluate_quality(p,now_ms=at(WINDOWS_PER_DAY))
    assert report["first_24h_available"]
    assert report["max_consecutive_missing_due_slots"]==21
    assert report["rolling_24h_missing_due_slots"]==21
    assert report["status"]=="PROSPECTIVE_WINDOW_OBSERVATION_DEGRADED"
    assert not report["provisional_24h_sample_gate_passed"]


def test_stale_existing_24h_archive_is_not_a_current_pass(tmp_path):
    p=tmp_path/"observe.sqlite"
    populate(p)
    report=evaluate_quality(p,now_ms=at(WINDOWS_PER_DAY)+3_600_000)
    assert report["first_24h_available"]
    assert report["seconds_since_last_clean_session"]>2400
    assert not report["provisional_24h_sample_gate_passed"]


def test_independent_reconnect_error_visible_and_is_not_own_fill(tmp_path):
    p=tmp_path/"observe.sqlite"
    populate(p,failed_slot=70)
    report=evaluate_quality(p,now_ms=at(WINDOWS_PER_DAY))
    assert report["reconnections_last_24h"]==1
    assert report["failed_degraded_sessions_last_24h"]==1
    assert report["rolling_24h_missing_due_slots"]==1
    assert report["books_per_market_and_receipt_lag"]["KRW-BTC"]["own_customer_fills"]==0


def test_storage_budget_guard_without_deleting_evidence(tmp_path,monkeypatch):
    import quantlab.websocket_quality as module
    p=tmp_path/"observe.sqlite"
    populate(p,count=3)
    monkeypatch.setattr(module,"WARNING_DAILY_DISK_BYTES",1)
    report=evaluate_quality(p,now_ms=at(2))
    assert report["daily_disk_budget_warning"]
    assert not report["first_24h_available"]
    assert report["retention_no_automatic_deletion"]
    # Clock in the CLI is the actual wall clock, not fake test clock:
    # invoke main only with time patch to test real exit status.
    monkeypatch.setattr(module.time,"time",lambda:at(2)/1000)
    assert main(["--db",str(p),"--enforce"])==2
    assert p.exists()


def test_service_runs_2h_crypto_raw_audit_before_24h_metadata(tmp_path):
    root=Path(__file__).resolve().parents[1]/"deploy"/"systemd"
    content=(root/"freequant-websocket-audit.service").read_text()
    assert content.count("ExecStart=")==2
    assert "websocket_microstructure --audit --audit-hours 2 --require-recent-seconds 2400" in content
    assert "websocket_quality --enforce" in content
    assert "WorkingDirectory=/home/hong/apps/FreeQuantLab-observer-runtime" in content
    assert "NoNewPrivileges=true" in content
    assert "ProtectSystem=strict" in content
    assert "ReadWritePaths=/home/hong/apps/FreeQuantLab/state/microstructure" in content


def test_invalid_missing_db_or_time_cannot_forge_quality_proof(tmp_path):
    with pytest.raises(FileNotFoundError):
        evaluate_quality(tmp_path/"unknown.sqlite")
    p=tmp_path/"ws.sqlite"
    open_db(p).close()
    with pytest.raises(ValueError,match="Invalid observation wall clock"):
        evaluate_quality(p,now_ms=0)

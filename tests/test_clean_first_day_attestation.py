"""Exact first matured clean-day SHA admission; synthetic DBs only."""
from __future__ import annotations
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3

import pytest
from test_clean_cohort import cohort,record  # noqa:F401

from quantlab.clean_first_day_attestation import AdmissionError,attest_first_day,main

PRE=datetime(2026,10,22,4,0,tzinfo=timezone.utc)
OPEN=datetime(2026,10,23,1,21,tzinfo=timezone.utc)


def test_before_due_never_seals_approval_or_touches_real_cohort(cohort,tmp_path):
    db,manifest=cohort
    target=tmp_path/"evidence"
    result=attest_first_day(db,manifest,target,now=PRE)
    assert result["status"]=="WAITING_FIRST_DUE_NOT_YET_MATURE"
    assert not result["receipt_created"]
    assert not target.exists()
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM shadow_events").fetchone()[0]==0


def test_complete_first_day_creates_one_immutable_sha_receipt_without_returns(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    receipts=tmp_path/"first-day"
    result=attest_first_day(db,manifest,receipts,now=OPEN)
    assert result["status"]=="FIRST_DAY_COMPLETE_VERIFIED_NO_PERFORMANCE_CLAIM"
    assert result["first_day_account_day_events"]==6
    assert result["first_day_public_quote_evidence"]==2
    assert result["receipt_created"]
    p=Path(result["receipt_path"])
    content=p.read_bytes()
    import hashlib
    assert hashlib.sha256(content).hexdigest()==result["receipt_sha256"]
    obj=json.loads(content)
    assert obj["read_only_shadow_evidence"]
    assert obj["public_last_trade_is_not_an_executable_fill"]
    assert obj["real_broker_orders"]==0
    assert "cash" not in obj and "equity" not in obj and "returns" not in obj
    again=attest_first_day(db,manifest,receipts,
                           now=datetime(2026,10,23,3,0,tzinfo=timezone.utc))
    assert again["receipt_sha256"]==result["receipt_sha256"]
    assert not again["receipt_created"]
    assert len(list(receipts.iterdir()))==1
    with sqlite3.connect(db) as c:
        assert c.execute("select count(*) from shadow_events").fetchone()[0]==6
        assert c.execute("select count(*) from shadow_quote_evidence").fetchone()[0]==2


def test_missing_first_day_no_acceptance_receipt(cohort,tmp_path):
    db,manifest=cohort
    out=tmp_path/"first-day"
    with pytest.raises(AdmissionError,match="did not pass"):
        attest_first_day(db,manifest,out,now=OPEN)
    assert not out.exists()


def test_mutated_quote_and_prior_attestation_never_overwritten(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    out=tmp_path/"first-day"
    first=attest_first_day(db,manifest,out,now=OPEN)
    p=Path(first["receipt_path"])
    original=p.read_bytes()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE shadow_quote_evidence SET public_ticker_price=1 WHERE symbol='KRW-ETH'")
    with pytest.raises(AdmissionError,match="did not pass"):
        attest_first_day(db,manifest,out,now=OPEN)
    assert p.read_bytes()==original


def test_replaced_preexisting_receipt_rejected_and_not_restored(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    out=tmp_path/"first-day"
    first=attest_first_day(db,manifest,out,now=OPEN)
    p=Path(first["receipt_path"])
    p.write_text('{"FAKE":true}')
    with pytest.raises(AdmissionError,match="conflicts"):
        attest_first_day(db,manifest,out,now=OPEN)
    assert p.read_text()=='{"FAKE":true}'


def test_symlink_destination_rejected(cohort,tmp_path):
    db,manifest=cohort
    record(db,manifest)
    real=tmp_path/"real"
    real.mkdir()
    destination=tmp_path/"first-day"
    destination.symlink_to(real,target_is_directory=True)
    with pytest.raises(AdmissionError,match="Symlink"):
        attest_first_day(db,manifest,destination,now=OPEN)


def test_cli_waits_without_network_or_writer(cohort,tmp_path,monkeypatch,capsys):
    import quantlab.clean_first_day_attestation as code
    monkeypatch.setattr(code,"_utc",lambda now=None:PRE)
    db,manifest=cohort
    receipt=tmp_path/"first-day"
    assert main(["--db",str(db),"--protocol",str(manifest),
                 "--receipt-dir",str(receipt)])==0
    assert "WAITING_FIRST_DUE_NOT_YET_MATURE" in capsys.readouterr().out
    assert not receipt.exists()
    monkeypatch.setattr(code,"_utc",lambda now=None:OPEN)
    assert main(["--db",str(db),"--protocol",str(manifest),
                 "--receipt-dir",str(receipt)])==2
    assert "FIRST_DAY_ACCEPTANCE_BLOCKED" in capsys.readouterr().err
    assert not receipt.exists()


def test_watchdog_service_runs_independent_seal_only_after_clean_watchdog_passes():
    root=Path(__file__).resolve().parents[1]
    txt=(root/"deploy/systemd/freequant-clean-watchdog.service").read_text()
    assert txt.count("ExecStart=")==2
    assert txt.index("quantlab.clean_cohort") < txt.index("quantlab.clean_first_day_attestation")
    assert "ProtectSystem=strict" in txt
    assert "ProtectHome=read-only" in txt
    assert "NoNewPrivileges=true" in txt
    assert "ReadWritePaths=/home/hong/apps/FreeQuantLab/state /home/hong/apps/FreeQuantLab/reports/clean-daily" in txt

def test_change_during_independent_audit_fails_closed(cohort,tmp_path,monkeypatch):
    import quantlab.clean_first_day_attestation as att
    db,manifest=cohort
    record(db,manifest)
    actual=att.inspect_clean
    def changing_audit(*args,**kwargs):
        result=actual(*args,**kwargs)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE shadow_quote_evidence SET public_ticker_price=public_ticker_price+1 WHERE symbol='KRW-ETH'")
        return result
    monkeypatch.setattr(att,"inspect_clean",changing_audit)
    dest=tmp_path/"receipt"
    with pytest.raises(AdmissionError,match="changed during independent audit"):
        attest_first_day(db,manifest,dest,now=OPEN)
    assert not dest.exists()

def test_change_between_hash_and_seal_fails_closed(cohort,tmp_path,monkeypatch):
    import quantlab.clean_first_day_attestation as att
    db,manifest=cohort
    record(db,manifest)
    original=att._first_day_snapshot
    calls=0
    def mutate_at_recheck(*args,**kwargs):
        nonlocal calls
        calls+=1
        events,quotes=original(*args,**kwargs)
        if calls==2:
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE shadow_quote_evidence SET public_ticker_price=public_ticker_price+1 WHERE symbol='KRW-BTC'")
        return events,quotes
    monkeypatch.setattr(att,"_first_day_snapshot",mutate_at_recheck)
    dest=tmp_path/"receipt"
    with pytest.raises(AdmissionError,match="changed before receipt"):
        attest_first_day(db,manifest,dest,now=OPEN)
    assert not dest.exists()

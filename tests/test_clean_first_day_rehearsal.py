"""Synthetic-only test of exact clean pre-registered first-day account identities."""
from __future__ import annotations

from datetime import datetime,timezone
import sqlite3

import pandas as pd
import pytest

from quantlab.clean_first_day_rehearsal import RehearsalError,rehearsal,main
from quantlab.prospective import read_protocol
from quantlab.shadow import run_cycle

from test_clean_cohort import cohort  # noqa:F401

NOW=datetime(2026,10,22,0,55,tzinfo=timezone.utc)


def test_exact_frozen_identity_first_day_rehearsal_disposable(cohort):
    db,manifest=cohort
    with sqlite3.connect(db) as con:
        count_before=con.execute("SELECT count(*) FROM shadow_runs").fetchone()[0]
    r=rehearsal(db,manifest,now=NOW)
    assert r["status"]=="SYNTHETIC_FIRST_DAY_REHEARSAL_PASS_NOT_REAL_MARKET"
    assert r["protocol_sha256"]==read_protocol(manifest)["sha256"]
    assert r["registered_real_accounts"]==6
    assert r["synthetic_target_decision_utc"]=="2026-10-22"
    assert r["synthetic_completed_account_days"]==6
    assert r["synthetic_public_quote_rows"]==2
    assert r["synthetic_independent_audit"]=="PASS"
    assert r["skip_day_alert_when_missing"]=="INCOMPLETE"
    assert r["synthetic_modified_quote_rejected"]
    assert r["repeated_run_duplicate_events"]==0
    assert r["late_restart_generated_events"]==0
    assert r["source_database_unchanged"]
    assert r["discarded_synthetic_database"]
    assert r["real_source_events_before_and_after"]==0
    assert not r["live_network_queried"]
    assert not r["historical_backfill_performed"]
    assert not r["automatic_strategy_promotion"]
    assert not r["real_broker_orders"]
    assert r["synthetic_virtual_order_count_not_performance"]>=0
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM shadow_events").fetchone()[0]==0
        assert con.execute("SELECT count(*) FROM shadow_quote_evidence").fetchone()[0]==0
        assert con.execute("SELECT count(*) FROM shadow_runs").fetchone()[0]==count_before


def test_rehearsal_rejects_past_decision_date(cohort):
    db,manifest=cohort
    with pytest.raises(RehearsalError,match="before first decision closes"):
        rehearsal(db,manifest,now=datetime(2026,10,23,0,35,tzinfo=timezone.utc))


def test_rehearsal_refuses_to_launder_real_prewindow_event(cohort):
    db,manifest=cohort
    # A single injected historic fill is enough to stop a first-day rehearsal.
    with sqlite3.connect(db) as con:
        a=con.execute("SELECT track_id FROM shadow_accounts LIMIT 1").fetchone()[0]
        con.execute("INSERT INTO shadow_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(
            a,"2026-10-20","2026-10-21","HOLD",0,100,0,0,1e6,0,1e6,
            "2026-10-21T00:24:00+00:00"))
    with pytest.raises(RehearsalError,match="Source audit failed"):
        rehearsal(db,manifest,now=NOW)


def test_rehearsal_rejects_tampered_protocol(cohort):
    db,manifest=cohort
    text=manifest.read_text()
    manifest.write_text(text.replace('"required_complete_utc_days": 90',
                                     '"required_complete_utc_days": 91'))
    with pytest.raises(ValueError,match="Protocol content hash"):
        rehearsal(db,manifest,now=NOW)


def test_rehearsal_cli_exit_is_strict_and_unprivileged(cohort,monkeypatch,capsys):
    import quantlab.clean_first_day_rehearsal as module
    monkeypatch.setattr(module,"_utc",lambda at=None: NOW)
    db,manifest=cohort
    assert main(["--db",str(db),"--protocol",str(manifest)])==0
    output=capsys.readouterr().out
    assert "SYNTHETIC_FIRST_DAY_REHEARSAL_PASS" in output
    assert main(["--db",str(db)+"-missing","--protocol",str(manifest)])==2
    assert "SYNTHETIC_REHEARSAL_FAILED_CLOSED" in capsys.readouterr().out

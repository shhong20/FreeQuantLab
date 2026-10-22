"""Synthetic-only tests: readiness must never change the registered cohort."""
from __future__ import annotations

from datetime import datetime,timezone
import sqlite3
import pytest

from test_clean_cohort import bars,cohort,record  # noqa: F401

from quantlab.clean_readiness import ReadinessError,inspect_readiness,main

PRE=datetime(2026,10,22,7,0,tzinfo=timezone.utc)
AFTER_OPEN=datetime(2026,10,23,0,11,tzinfo=timezone.utc)


def _count(db):
    with sqlite3.connect(db) as conn:
        return tuple(conn.execute("SELECT count(*) FROM "+table).fetchone()[0]
                     for table in ("shadow_accounts","shadow_events",
                                   "shadow_quote_evidence","shadow_runs"))


def test_offline_readiness_is_read_only_without_public_calls(cohort):
    db,protocol=cohort
    before=_count(db)
    result=inspect_readiness(db,protocol,now=PRE,
        fetch_fn=lambda _: pytest.fail("Offline preflight fetched public market"),
        quote_fn=lambda *a,**k: pytest.fail("Offline preflight fetched quote"))
    assert result["status"]=="READY_LEDGER_ONLY"
    assert not result["online_market_checked"]
    assert result["frozen_accounts"]==6
    assert result["simulated_events"]==result["quote_evidence_rows"]==0
    assert result["first_day_accepted"] is False
    assert result["real_orders_enabled"] is False
    assert result["automatic_strategy_promotion"] is False
    assert _count(db)==before


def test_online_public_market_validation_does_not_capture_quotes(cohort):
    db,protocol=cohort
    frame=bars("2026-10-21")
    calls=[]
    def public_fetch(asset):
        calls.append(("daily",asset["symbol"]))
        return frame.copy()
    def public_ticker(provider,symbol,*,now):
        calls.append(("ticker",symbol))
        return float(frame["close"].iloc[-1]),now.date().isoformat()
    before=_count(db)
    report=inspect_readiness(db,protocol,now=PRE,online=True,
                             fetch_fn=public_fetch,quote_fn=public_ticker)
    assert report["status"]=="READY_MARKET_VALIDATED"
    assert len(report["public_market_probes"])==2
    assert {r["completed_utc_day"] for r in report["public_market_probes"]}=={"2026-10-21"}
    assert calls==[("daily","KRW-BTC"),("ticker","KRW-BTC"),
                   ("daily","KRW-ETH"),("ticker","KRW-ETH")]
    assert _count(db)==before


def test_stale_bar_blocks_online_readiness_without_mutation(cohort):
    db,protocol=cohort
    before=_count(db)
    with pytest.raises(ValueError,match="yesterday"):
        inspect_readiness(db,protocol,now=PRE,online=True,
                          fetch_fn=lambda _:bars("2026-10-20"))
    assert _count(db)==before


def test_stale_ticker_session_blocks_online_readiness(cohort):
    db,protocol=cohort
    frame=bars("2026-10-21")
    before=_count(db)
    with pytest.raises(ReadinessError,match="session mismatch"):
        inspect_readiness(db,protocol,now=PRE,online=True,
                          fetch_fn=lambda _:frame.copy(),
                          quote_fn=lambda *a,**k:
                          (float(frame["close"].iloc[-1]),"2026-10-21"))
    assert _count(db)==before


def test_existing_events_are_never_misrepresented_as_preflight_ready(cohort):
    db,protocol=cohort
    record(db,protocol)
    with pytest.raises(ReadinessError,match="Contaminated"):
        inspect_readiness(db,protocol,now=PRE)


def test_preflight_expires_at_first_observation_window(cohort):
    db,protocol=cohort
    with pytest.raises(ReadinessError,match="expired"):
        inspect_readiness(db,protocol,now=AFTER_OPEN)


def test_readonly_cli_exit_status(cohort,monkeypatch,capsys):
    import quantlab.clean_readiness as mod
    db,protocol=cohort
    monkeypatch.setattr(mod,"_utc",lambda at=None:PRE)
    assert main(["--db",str(db),"--protocol",str(protocol)])==0
    assert "READY_LEDGER_ONLY" in capsys.readouterr().out
    monkeypatch.setattr(mod,"_utc",lambda at=None:AFTER_OPEN)
    assert main(["--db",str(db),"--protocol",str(protocol)])==2
    assert "READINESS_BLOCKED" in capsys.readouterr().err

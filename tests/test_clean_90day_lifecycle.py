"""Full 90-day, offline-only proof of prospective clean cohort ledger rules.

Only disposable pytest SQLite databases and synthetic public candles/tickers.
No live market calls, physical broker orders, real DB writes, or returns claims.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import sqlite3

from test_clean_cohort import ASSETS, bars, cohort  # noqa: F401

from quantlab.clean_cohort import inspect_clean
from quantlab.clean_first_day_attestation import attest_first_day
from quantlab.prospective import read_protocol
from quantlab.shadow import run_cycle

UTC = timezone.utc


def _observe(db, protocol, decision: date, *, minute: int = 24):
    instant = datetime.combine(decision + timedelta(days=1), time(0, minute), UTC)
    candles = bars(decision.isoformat())
    price = float(candles["close"].iloc[-1])
    return run_cycle(
        db, ASSETS, now=instant,
        fetch_fn=lambda asset: candles.copy(),
        quote_fn=lambda provider, symbol, *, now: (price, now.date().isoformat()),
        protocol_path=protocol,
    )


def _counts(db):
    with sqlite3.connect(db) as con:
        return tuple(con.execute("SELECT count(*) FROM " + table).fetchone()[0]
                     for table in ("shadow_accounts", "shadow_events",
                                   "shadow_quote_evidence"))


def test_all_ninety_due_days_are_complete_reconciled_and_first_seal_is_stable(cohort, tmp_path):
    db, protocol = cohort
    plan = read_protocol(protocol)
    first = date.fromisoformat(plan["first_eligible_decision_utc"])
    last = date.fromisoformat(plan["last_eligible_decision_utc"])
    assert last - first == timedelta(days=89)
    receipt_dir = tmp_path / "first-day"
    first_digest = None
    for offset in range(90):
        decision = first + timedelta(days=offset)
        summary = _observe(db, protocol, decision)
        assert summary["status"] == "OK", (offset, summary["errors"])
        assert sum(r.get("status") == "RECORDED" for r in summary["results"]) == 6
        if offset in (0, 44, 89):
            duplicate = _observe(db, protocol, decision, minute=34)
            assert duplicate["status"] == "OK"
            assert all(r.get("status") == "WAIT_NEW_BAR_OR_ALREADY_RECORDED"
                       for r in duplicate["results"])
        audit_at = datetime.combine(decision + timedelta(days=1), time(1, 21), UTC)
        if offset in (0, 44, 89):
            report = inspect_clean(db, protocol, now=audit_at)
            assert report["status"] == "PASS", report["issues"]
            assert report["expected_due_account_days"] == 6 * (offset + 1)
            assert report["recorded_due_account_days"] == 6 * (offset + 1)
            assert report["invalid_due_account_days"] == 0
            assert report["strategy_returns"] is None
            assert not report["automatic_strategy_promotion"]
        if offset == 0:
            first_report = attest_first_day(db, protocol, receipt_dir, now=audit_at)
            assert first_report["receipt_created"]
            first_digest = first_report["receipt_sha256"]
            assert _counts(db) == (6, 6, 2)
    assert _counts(db) == (6, 540, 180)
    assert first_digest is not None
    final_at = datetime.combine(last + timedelta(days=1), time(1, 21), UTC)
    repeat = attest_first_day(db, protocol, receipt_dir, now=final_at)
    assert repeat["receipt_created"] is False
    assert repeat["receipt_sha256"] == first_digest
    assert len(list(receipt_dir.glob("first-day-*.json"))) == 1
    final_report = inspect_clean(db, protocol, now=final_at)
    assert final_report["status"] == "PASS"
    assert final_report["expected_due_account_days"] == 540
    assert final_report["recorded_due_account_days"] == 540
    assert final_report["quote_proxy_status"] == "PASS"
    assert final_report["ledger_audit_status"] == "PASS"
    assert not final_report["real_orders_enabled"]
    after_window = _observe(db, protocol, last + timedelta(days=1))
    assert after_window["status"] == "OK"
    assert after_window["results"][0]["status"] == "OUTSIDE_PROTOCOL_WINDOW"
    assert _counts(db) == (6, 540, 180)


def test_irrecoverable_missing_due_day_detected_through_end_of_study(cohort):
    db, protocol = cohort
    first = date.fromisoformat(read_protocol(protocol)["first_eligible_decision_utc"])
    assert _observe(db, protocol, first)["status"] == "OK"
    # Skip second due day entirely; no historical ticker may be fabricated.
    for offset in range(2, 90):
        assert _observe(db, protocol, first + timedelta(days=offset))["status"] == "OK"
    final = datetime.combine(first + timedelta(days=90), time(1, 21), UTC)
    report = inspect_clean(db, protocol, now=final)
    assert report["status"] == "INCOMPLETE"
    assert report["first_incomplete_decision_utc"] == (
        first + timedelta(days=1)).isoformat()
    assert "CUMULATIVE_DUE_ACCOUNT_DAYS_MISSING" in report["issues"]
    assert report["expected_due_account_days"] == 540
    assert report["recorded_due_account_days"] == 534
    assert _counts(db) == (6, 534, 178)
    assert not report["automatic_strategy_promotion"]


def test_old_quote_modified_after_ninety_days_never_approves(cohort, tmp_path):
    """Forensic replay rejects late corruption of an old public price."""
    from quantlab.clean_first_day_attestation import AdmissionError
    import pytest
    db, protocol = cohort
    first = date.fromisoformat(read_protocol(protocol)["first_eligible_decision_utc"])
    receipt_dir = tmp_path / "first-day"
    for offset in range(90):
        assert _observe(db, protocol, first + timedelta(days=offset))["status"] == "OK"
        if offset == 0:
            day_one = datetime.combine(first + timedelta(days=1), time(1, 21), UTC)
            receipt = attest_first_day(db, protocol, receipt_dir, now=day_one)
    path = receipt_dir / ("first-day-" + first.isoformat() + ".json")
    original = path.read_bytes()
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE shadow_quote_evidence "
            "SET public_ticker_price=public_ticker_price+1 "
            "WHERE symbol=? AND decision_date=?",
            ("KRW-ETH", (first + timedelta(days=42)).isoformat()),
        )
    final = datetime.combine(first + timedelta(days=90), time(1, 21), UTC)
    audit = inspect_clean(db, protocol, now=final)
    assert audit["status"] == "INCOMPLETE"
    assert audit["quote_proxy_status"] == "FAIL"
    assert "CUMULATIVE_QUOTE_OR_TIMESTAMP_EVIDENCE_INVALID" in audit["issues"]
    with pytest.raises(AdmissionError, match="did not pass"):
        attest_first_day(db, protocol, receipt_dir, now=final)
    assert path.read_bytes() == original
    assert receipt["receipt_created"]
    assert _counts(db) == (6, 540, 180)

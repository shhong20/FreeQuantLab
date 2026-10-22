"""Prevent the clean 90-day lane from silently moving to a development branch.

The original pre-registered protocol and live ledger were frozen 2026-10-21.
These assertions validate checked-in unit contracts; CI does not run real
market services nor touch private or real broker accounts.
"""
from __future__ import annotations
from pathlib import Path

from quantlab.prospective import read_protocol

ROOT=Path(__file__).resolve().parents[1]
UNIT=ROOT/"deploy"/"systemd"
PINNED="/home/hong/apps/FreeQuantLab-clean-runtime"
PERSISTENT="/home/hong/apps/FreeQuantLab/state"


def test_immutable_predeclared_90d_identity_is_git_versioned():
    file=ROOT/"protocols"/"2026-10-21-btc-eth-clean-90d.json"
    plan=read_protocol(file)
    assert plan["sha256"]=="6f5e9cf6333d3dd5fe4339ef9d7ba401df36bc361915584bf45226f464cc5054"
    assert plan["first_eligible_decision_utc"]=="2026-10-22"
    assert plan["last_eligible_decision_utc"]=="2027-01-19"
    assert plan["required_complete_utc_days"]==90
    assert plan["evaluation_not_before_utc"]=="2027-01-20T00:00:00+00:00"
    assert len(plan["account_set"])==6
    assert {x["symbol"] for x in plan["account_set"]}=={"KRW-BTC","KRW-ETH"}
    assert plan["real_broker_order_permission"] is False
    assert plan["automatic_strategy_promotion"] is False


def test_user_services_read_pinned_git_source_but_write_only_immutable_separate_cohort():
    shadow=(UNIT/"freequant-clean-shadow.service").read_text()
    watchdog=(UNIT/"freequant-clean-watchdog.service").read_text()
    for txt in (shadow,watchdog):
        assert f"WorkingDirectory={PINNED}" in txt
        assert f"--protocol {PINNED}/protocols/2026-10-21-btc-eth-clean-90d.json" in txt
        assert f"--db {PERSISTENT}/clean_shadow_90d.db" in txt
        assert f"ReadWritePaths={PERSISTENT}" in txt
        assert "NoNewPrivileges=true" in txt
        assert "ProtectSystem=strict" in txt
        assert "ProtectHome=read-only" in txt
        assert "sudo " not in txt
        assert "--live-order" not in txt
        assert "--promotion" not in txt
        assert "--backfill" not in txt
        assert "state/shadow.db" not in txt
        assert "qlib_lab_shadow.db" not in txt
    assert f"--config {PINNED}/examples/clean_shadow_crypto.json" in shadow
    assert f"--alert-db {PERSISTENT}/clean-watchdog.db" in watchdog
    assert "real_orders_enabled" not in shadow # never flips account permissions


def test_catchup_schedule_cannot_paper_trade_outside_valid_utc_close():
    shadow=(UNIT/"freequant-clean-shadow.timer").read_text()
    watchdog=(UNIT/"freequant-clean-watchdog.timer").read_text()
    for kst in ("09:24:00","09:34:00","09:44:00","10:10:00"):
        assert kst in shadow
    assert "Persistent=true" in shadow
    assert "OnCalendar=" in watchdog and "10:50:00" in watchdog
    assert "12:50:00" in watchdog and "Persistent=true" in watchdog
    assert "OnStartupSec=8min" in watchdog

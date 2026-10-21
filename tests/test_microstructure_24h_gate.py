"""Rolling 24h capture gate: data continuity != real fill calibration."""
from __future__ import annotations

from datetime import timedelta

import pytest

from quantlab.microstructure_24h_gate import (
    assess_rolling_24h,EXPECTED_DAILY_SLOTS,
)
from test_microstructure_observer import do, clock_at


def test_initial_live_research_not_mislabeled_as_24h(tmp_path):
    path=tmp_path/"small.db"
    start=4_500_000
    for i in range(7):
        do(path,clock_at(start+i))
    status=assess_rolling_24h(path,now=clock_at(start+6))
    assert status["status"]=="COLLECTING_NOT_YET_24_HOURS"
    assert status["validated_collected_slots_in_window"]<=7
    assert status["expected_10min_slots"]==144
    assert not status["observation_gate_passed"]
    assert not status["real_order_fill_accuracy_verified"]


def test_contiguous_24h_pass_means_only_visible_depth_coverage(tmp_path):
    path=tmp_path/"complete.db"
    start=4_550_000
    for i in range(EXPECTED_DAILY_SLOTS+1):
        do(path,clock_at(start+i))
    result=assess_rolling_24h(path,now=clock_at(start+EXPECTED_DAILY_SLOTS))
    assert result["elapsed_24h_criterion_met"]
    assert result["validated_collected_slots_in_window"]==144
    assert result["missing_due_slots"]==0
    assert result["slot_coverage_fraction"]==pytest.approx(1.)
    assert result["status"]=="24H_VISIBLE_L2_COVERAGE_PASS_NOT_REAL_FILL_CALIBRATION"
    assert result["observation_gate_passed"]
    assert all(v["distinct_verified_exchange_book_timestamps"]>=100
               for v in result["all_four_asset_depth_quality"].values())
    assert not result["model_calibration_allowed"]
    assert not result["real_orders_enabled"]


def test_long_outage_causes_degraded_window_even_after_24h_elapsed(tmp_path):
    path=tmp_path/"outage.db"
    start=4_600_000
    # Drop 16 due observations in the latest 24h window.
    for i in range(EXPECTED_DAILY_SLOTS+1):
        if 50<=i<66:
            continue
        do(path,clock_at(start+i))
    result=assess_rolling_24h(path,now=clock_at(start+EXPECTED_DAILY_SLOTS))
    assert result["elapsed_24h_criterion_met"]
    assert result["validated_collected_slots_in_window"]==144-16
    assert result["slot_coverage_fraction"]<.9
    assert result["longest_missing_run_slots"]==16
    assert result["status"]=="24H_VISIBLE_L2_COVERAGE_DEGRADED"
    assert result["first_15_missing_due_slot_ids"]


def test_stale_last_success_never_passes_even_if_past_was_perfect(tmp_path):
    path=tmp_path/"halted.db"
    start=4_650_000
    for i in range(EXPECTED_DAILY_SLOTS+1):
        do(path,clock_at(start+i))
    now=clock_at(start+EXPECTED_DAILY_SLOTS)+timedelta(minutes=45)
    result=assess_rolling_24h(path,now=now)
    assert result["elapsed_24h_criterion_met"]
    assert not result["recent_data_fresh"]
    assert not result["observation_gate_passed"]
    assert result["status"]=="24H_VISIBLE_L2_COVERAGE_DEGRADED"

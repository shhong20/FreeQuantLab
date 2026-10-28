"""KRX source triage: raw provenance, close price parity, missing-label holds."""
from __future__ import annotations

import pytest

from quantlab.intraday_krx_5m_preview import CLOCKS
from quantlab.intraday_krx_source_triage import inspect_one
from quantlab.intraday_kis_venues import VenueProbeError


def inputs(*, missing=(), marker="153000", daily_close="103"):
    symbol="005930"
    session="2026-10-23"
    observed={
        clock:{"stck_cntg_hour":clock,
               "stck_prpr":"100","stck_oprc":"100"}
        for clock in CLOCKS if clock not in missing
    }
    observed[marker]={"stck_cntg_hour":marker,"stck_prpr":"103",
                       "stck_oprc":"103"}
    risk={
        "session":session,"symbol":symbol,
        "source_minute_sha256":"a"*64,
        "missing_regular_minute_count":len(missing),
        "observed_closing_clock_candidates":[marker],
        "day_disposition":"ENTIRE_DAY_STRATEGY_PNL_BLOCKED_UNKNOWN_GAPS",
    }
    provider={
        "market":"J","session":session,"symbol":symbol,
        "source_minute_bar_sha256":"a"*64,
        "pages":4,
        "pagination_stop":"short_page_unproven_earlier_history",
        "sample_first_labels":sorted(observed)[:3],
        "daily_comparison":{
            "requested_date_matches":1,
            "date_only_public_fields":[{
                "stck_bsop_date":"20261023",
                "stck_clpr":daily_close,
            }],
        },
    }
    return provider, observed, risk


def one(*, missing=(), marker="153000", daily_close="103"):
    provider,observed,risk=inputs(missing=missing,marker=marker,
                                  daily_close=daily_close)
    return inspect_one(symbol="005930",session="2026-10-23",
                       report=provider,observed=observed,day=risk)


def test_missing_opening_bar_keeps_null_not_fake_trade():
    record=one(missing=("090000","090100"))
    assert record["unknown_regular_minute_labels"]==["090000","090100"]
    assert record["unknown_minute_neighbors_not_imputations"][0]["before_clock"] is None
    assert record["unknown_minute_neighbors_not_imputations"][0]["after_clock"]=="090200"
    assert record["unknown_minute_neighbors_not_imputations"][0]["before_close"] is None
    assert record["missing_clocks_are_zero_trading"] is False
    assert record["corrections_or_order_execution_allowed"] is False


def test_1532_equal_daily_close_never_verifies_timestamp():
    record=one(marker="153200")
    assert record["last_observed_close_matches_daily"] is True
    assert record["close_marker_is_153200"] is True
    assert record["closing_auction_time_semantics_verified"] is False


def test_daily_price_disagreement_is_explicit_and_no_auto_relabel():
    record=one(marker="153200",daily_close="104")
    assert record["last_observed_close_matches_daily"] is False
    assert record["last_close_label_price"]==103
    assert record["unadjusted_daily_close"]==104


@pytest.mark.parametrize("change",["sha","count","marker","wrong_day"])
def test_corrupted_source_identity_or_audit_blocks(change):
    provider,observed,risk=inputs(missing=("090000",))
    if change=="sha":
        provider["source_minute_bar_sha256"]="b"*64
    if change=="count":
        risk["missing_regular_minute_count"]=2
    if change=="marker":
        risk["observed_closing_clock_candidates"]=["153200"]
    if change=="wrong_day":
        provider["daily_comparison"]["date_only_public_fields"][0][
            "stck_bsop_date"]="20261022"
    with pytest.raises(VenueProbeError):
        inspect_one(symbol="005930",session="2026-10-23",
                    report=provider,observed=observed,day=risk)

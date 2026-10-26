"""Synthetic unit tests of phase coverage and algebraic volume bridge."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_kis_phase_coverage import audit, clock_labels
from quantlab.intraday_kis_venues import capture, VenueProbeError

NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)


class Response:
    def __init__(self, obj):
        self.content = json.dumps(obj).encode()

    def raise_for_status(self):
        return None


def mock_rows(market, mismatch=False):
    definitions = {
        "J": [("090000", 100), ("153000", 100), ("160000", 100)],
        "NX": [("080000", 100), ("090000", 100),
               ("154000", 100), ("160000", 100)],
        "UN": [("080000", 100), ("090000", 200),
               ("153000", 100), ("154000", 100), ("160000", 200)],
    }
    entries = []
    for tm, volume in definitions[market]:
        if mismatch and market == "UN" and tm == "090000":
            volume -= 20
        entries.append({
            "stck_bsop_date": "20261023",
            "stck_cntg_hour": tm,
            "stck_oprc": "100",
            "stck_hgpr": "100",
            "stck_lwpr": "100",
            "stck_prpr": "100",
            "cntg_vol": str(volume),
        })
    return entries


def fill(root, markets=("J", "NX", "UN"), *, mismatch=False):
    for market in markets:
        def get(url, **kwargs):
            assert kwargs["params"]["FID_COND_MRKT_DIV_CODE"] == market
            if url.endswith("inquire-time-dailychartprice"):
                return Response({"rt_cd": "0", "output2": mock_rows(market, mismatch)})
            return Response({"rt_cd": "0", "output2": [{
                "stck_bsop_date": "20261023",
                "acml_vol": str({"J":1000,"NX":600,"UN":1600}[market]),
                "stck_clpr": "100",
            }]})
        capture(symbol="005930", session="2026-10-23", market=market,
                out_dir=root, key="DUMMY_KEY", secret="DUMMY_SECRET",
                token="DUMMY_BEARER", getter=get, now=NOW)


def test_fixed_price_absence_and_daily_gap_are_not_imputed(tmp_path):
    source=tmp_path/"source"
    fill(source)
    output=audit(source_root=source,symbols=("005930",),
                 sessions=("2026-10-23",),out_dir=tmp_path/"report")
    result=output["summary"]
    assert result["compared_case_count"] == 1
    assert result["unmatched_case_count"] == 0
    assert result["all_accounting_identities_hold"] is True
    assert result["all_official_daily_market_volume_sums_match"] is True
    assert result["krx_fixed_price_clock_labels_absent_in_every_compared_case"] is True
    sample=result["cases"][0]
    assert sample["market"]["J"]["daily_minus_minute_volume"] == 700
    assert sample["market"]["NX"]["daily_minus_minute_volume"] == 200
    assert sample["market"]["UN"]["daily_minus_minute_volume"] == 900
    windows={w["code"]:w for w in sample["market"]["J"]["calendar_window_coverage"]}
    assert windows["KRX_PRE_OPEN_FIXED_PRICE"]["present_labels"] == 0
    assert windows["KRX_AFTER_FIXED_PRICE"]["absent_labels"] == 20
    assert windows["KRX_AFTER_FIXED_PRICE"]["absence_is_not_proof_of_zero_trades"]
    assert result["no_missing_phase_volume_imputation"] is True
    assert result["usable_for_backtesting"] is False
    assert sample["timestamp_bucket_start_or_end"] == "NOT_VENDOR_VERIFIED"
    p=Path(output["output"])
    assert sha256(p.read_bytes()).hexdigest() == (
        p.with_suffix(".sha256").read_text().strip()
    )
    with pytest.raises(VenueProbeError, match="already exists"):
        audit(source_root=source,symbols=("005930",),sessions=("2026-10-23",),
              out_dir=tmp_path/"report")


def test_minute_discrepancy_identity_is_algebra_not_trade_proof(tmp_path):
    source=tmp_path/"source"
    fill(source, mismatch=True)
    s=audit(source_root=source,symbols=("005930",),
            sessions=("2026-10-23",),out_dir=tmp_path/"report")["summary"]
    c=s["cases"][0]
    assert c["minute_volume_un_minus_j_plus_nx"] == -20
    assert c["market"]["UN"]["daily_minus_minute_volume"] == 920
    assert c["daily_gap_accounting_identity_holds"] is True
    assert c["accounting_identity_is_only_algebra_not_execution_attribution"] is True
    assert c["eligible_for_backtest"] is False


def test_missing_triple_market_not_counted_as_stable(tmp_path):
    source=tmp_path/"source"
    fill(source, markets=("J","NX"))
    result=audit(source_root=source,symbols=("005930",),
                 sessions=("2026-10-23",),out_dir=tmp_path/"report")["summary"]
    assert result["compared_case_count"]==0
    assert result["unmatched_case_count"]==1
    assert result["all_accounting_identities_hold"] is False
    assert result["unmatched_cases"][0]["missing_venues"]==["UN"]


def test_invalid_identifiers_rejected_without_creating_output(tmp_path):
    with pytest.raises(VenueProbeError):
        audit(source_root=tmp_path,symbols=("../hack",),
              sessions=("2026-10-23",),out_dir=tmp_path/"report")
    assert not (tmp_path/"report").exists()


def test_fixed_clock_range_contract():
    assert len(clock_labels(9,0,15,20)) == 380
    assert len(clock_labels(15,40,16,0)) == 20
    assert len(clock_labels(16,0,20,0)) == 240
    assert clock_labels(15,40,15,42) == ("154000", "154100")
    with pytest.raises(VenueProbeError):
        clock_labels(20,0,16,0)

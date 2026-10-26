"""Offline three-market source intersection tests, no real API key or network."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_kis_venues import capture, VenueProbeError
from quantlab.intraday_kis_venue_compare import audit_markets


NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
SESSION = "2026-10-23"
TICKER = "005930"


class Resp:
    def __init__(self, obj):
        self.content = json.dumps(obj).encode()

    def raise_for_status(self):
        pass


def make_getter(market, *, consistent_integrated=False):
    def get(url, **kw):
        if url.endswith("inquire-time-dailychartprice"):
            assert kw["params"]["FID_COND_MRKT_DIV_CODE"] == market
            return Resp({"rt_cd": "0", "output2": [
                {"stck_bsop_date": "20261023", "stck_cntg_hour": clock,
                 "stck_oprc": str(price), "stck_hgpr": str(price),
                 "stck_lwpr": str(price), "stck_prpr": str(price),
                 "cntg_vol": ("200" if consistent_integrated and market == "UN"
                             and clock in ("090000", "155500") else "100")}
                for clock,price in (
                    [("090000", 100), ("155500", 101)]
                    if market == "J" else
                    [("080000", 97), ("090000", 99), ("155500", 101)]
                    if market == "NX" else
                    [("080000", 98), ("090000", 99), ("155500", 102)]
                )
            ]})
        return Resp({"rt_cd": "0", "output2": [
            {"stck_bsop_date": "20261023", "stck_clpr": "102",
             "acml_vol": "600" if market == "UN" else "300"}
        ]})
    return get


def populate(tmp_path, *, consistent_integrated=False):
    root = tmp_path / "source"
    for market in ("J", "NX", "UN"):
        capture(
            symbol=TICKER, session=SESSION, market=market, out_dir=root,
            key="DEMO_KEY", secret="DEMO_SECRET", token="DEMO_TOKEN",
            getter=make_getter(market, consistent_integrated=consistent_integrated), now=NOW,
        )
    return root


def test_venue_comparison_does_not_fabricate_market_trades(tmp_path):
    source = populate(tmp_path)
    result = audit_markets(root=source, symbol=TICKER, session=SESSION,
                           out_dir=tmp_path/"comparison")
    report = result["audit"]
    assert report["status"] == "KIS_J_NX_UN_SOURCE_COMPARISON_NOT_TRADE_RECONCILIATION"
    assert report["same_clock_label_overlaps_not_duplicate_trades"] == {
        "J_NX": 2, "J_UN": 2, "NX_UN": 3, "ALL": 2,
    }
    assert report["integrated_last_price_label_comparison"]["J"]["same_reported_last_price_labels"] == 0
    assert report["integrated_last_price_label_comparison"]["NX"]["same_reported_last_price_labels"] == 1
    assert report["integrated_volume_equals_sum_of_venues"] == "NOT_ASSUMED"
    assert report["venue_attribution"] == "NOT_DETERMINABLE_FROM_MINUTE_OHLCV"
    assert report["usable_for_backtesting"] is False
    assert report["usable_for_paper_or_live"] is False
    assert len(report["official_daily_unadjusted_raw_audits"]) == 3
    min_check = report["minute_volume_arithmetic_diagnostic"]
    day_check = report["official_daily_volume_arithmetic_diagnostic"]
    assert min_check["status"] == "MISMATCH_HOLD"
    assert min_check["mismatch_count"] == 2
    assert min_check["minute_integrated_minus_venue_sum"] == -200
    assert [m["label"] for m in min_check["mismatches"]] == ["090000", "155500"]
    assert day_check["status"] == "ARITHMETIC_MATCH_ONLY"
    assert day_check["integrated_minus_venue_volume"] == 0
    assert day_check["minute_minus_official_daily_volume"] == {
        "J": -100, "NX": 0, "UN": -300
    }
    assert "INTEGRATED_MINUTE_VOLUME_MISMATCH" in report["source_quality_holds"]
    assert report["minute_price_envelope_diagnostic"]["status"] == "ARITHMETIC_MATCH_ONLY"
    assert report["minute_price_envelope_diagnostic"]["mismatch_count"] == 0
    assert day_check["approved"] is False
    assert Path(result["output"]).is_dir()
    with pytest.raises(VenueProbeError, match="already exists"):
        audit_markets(root=source, symbol=TICKER, session=SESSION,
                      out_dir=tmp_path/"comparison")


def test_comparison_fails_closed_on_missing_market(tmp_path):
    source = populate(tmp_path)
    target = source / f"KIS_NX_EXT_{TICKER}_{SESSION}"
    target.rename(source / "moved_NX")
    with pytest.raises(VenueProbeError, match="Cannot verify"):
        audit_markets(root=source, symbol=TICKER, session=SESSION,
                      out_dir=tmp_path/"comparison")


def test_comparison_fails_closed_on_modified_minute_bytes(tmp_path):
    source = populate(tmp_path)
    target = source / f"KIS_UN_EXT_{TICKER}_{SESSION}" / "minute_observations_UNVERIFIED.json"
    target.write_text(target.read_text().replace("102", "103"))
    with pytest.raises(VenueProbeError, match="digest mismatch"):
        audit_markets(root=source, symbol=TICKER, session=SESSION,
                      out_dir=tmp_path/"comparison")


def test_equal_minute_sums_do_not_prove_trade_identity(tmp_path):
    source = populate(tmp_path, consistent_integrated=True)
    report = audit_markets(
        root=source, symbol=TICKER, session=SESSION,
        out_dir=tmp_path / "comparison",
    )["audit"]
    minute = report["minute_volume_arithmetic_diagnostic"]
    daily = report["official_daily_volume_arithmetic_diagnostic"]
    assert minute["status"] == "ARITHMETIC_MATCH_ONLY"
    assert minute["mismatch_count"] == 0
    assert minute["minute_integrated_minus_venue_sum"] == 0
    assert minute["execution_identity_verified"] is False
    assert minute["approved"] is False
    assert daily["status"] == "ARITHMETIC_MATCH_ONLY"
    assert report["venue_attribution"] == "NOT_DETERMINABLE_FROM_MINUTE_OHLCV"
    assert report["usable_for_backtesting"] is False


def test_integrated_minute_price_envelope_violation_is_independent_hard_hold(tmp_path):
    source = populate(tmp_path, consistent_integrated=True)
    snapshot = source / f"KIS_UN_EXT_{TICKER}_{SESSION}"
    rowfile = snapshot / "minute_observations_UNVERIFIED.json"
    observations = json.loads(rowfile.read_text())
    for row in observations:
        if row["stck_cntg_hour"] == "090000":
            row["stck_hgpr"] = "95"
            row["stck_oprc"] = "95"
            row["stck_lwpr"] = "95"
            row["stck_prpr"] = "95"
    data = (json.dumps(observations, ensure_ascii=False, sort_keys=True, indent=2)+"\n").encode()
    rowfile.write_bytes(data)
    # Only for testing independently inconsistent but internally hashed vendor data.
    auditfile = snapshot / "audit.json"
    audit = json.loads(auditfile.read_text())
    audit["source_minute_bar_sha256"] = sha256(data).hexdigest()
    auditfile.write_text(json.dumps(audit))
    out = audit_markets(root=source, symbol=TICKER, session=SESSION,
                        out_dir=tmp_path/"comparison")["audit"]
    prices = out["minute_price_envelope_diagnostic"]
    assert prices["status"] == "MISMATCH_HOLD"
    assert prices["mismatch_count"] == 1
    assert prices["mismatches"][0]["label"] == "090000"
    assert "INTEGRATED_MINUTE_PRICE_ENVELOPE_MISMATCH" in out["source_quality_holds"]
    assert out["minute_volume_arithmetic_diagnostic"]["mismatch_count"] == 0
    assert out["usable_for_backtesting"] is False

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


def make_getter(market):
    def get(url, **kw):
        if url.endswith("inquire-time-dailychartprice"):
            assert kw["params"]["FID_COND_MRKT_DIV_CODE"] == market
            return Resp({"rt_cd": "0", "output2": [
                {"stck_bsop_date": "20261023", "stck_cntg_hour": clock,
                 "stck_oprc": str(price), "stck_hgpr": str(price),
                 "stck_lwpr": str(price), "stck_prpr": str(price),
                 "cntg_vol": "100"}
                for clock,price in (
                    [("090000", 100), ("155500", 101)]
                    if market == "J" else
                    [("080000", 97), ("090000", 99), ("155500", 101)]
                    if market == "NX" else
                    [("080000", 98), ("090000", 99), ("155500", 102)]
                )
            ]})
        return Resp({"rt_cd": "0", "output2": [
            {"stck_bsop_date": "20261023", "stck_clpr": "102", "acml_vol": "300"}
        ]})
    return get


def populate(tmp_path):
    root = tmp_path / "source"
    for market in ("J", "NX", "UN"):
        capture(
            symbol=TICKER, session=SESSION, market=market, out_dir=root,
            key="DEMO_KEY", secret="DEMO_SECRET", token="DEMO_TOKEN",
            getter=make_getter(market), now=NOW,
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
    assert report["integrated_last_price_label_comparison"]["NX"]["same_reported_last_price_labels"] == 2
    assert report["integrated_volume_equals_sum_of_venues"] == "NOT_ASSUMED"
    assert report["venue_attribution"] == "NOT_DETERMINABLE_FROM_MINUTE_OHLCV"
    assert report["usable_for_backtesting"] is False
    assert report["usable_for_paper_or_live"] is False
    assert len(report["official_daily_unadjusted_raw_audits"]) == 3
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

"""KIS historical J/NX/UN market isolation using mocked vendor responses."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_kis_venues import (
    VenueProbeError, capture, collect_minute, phase, MARKETS,
)


NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
SESSION = "2026-10-23"
TICKER = "005930"


def bar(hour: str, *, price="100", day="20261023", volume="10"):
    return {
        "stck_bsop_date": day, "stck_cntg_hour": hour,
        "stck_oprc": price, "stck_hgpr": price,
        "stck_lwpr": price, "stck_prpr": price, "cntg_vol": volume,
    }


class Resp:
    def __init__(self, value):
        self.content = json.dumps(value).encode()

    def raise_for_status(self):
        return None


def responder(minute_rows, daily_rows=None, *, provider_daily_error=None):
    calls = []
    def get(url, **kw):
        calls.append((url, kw))
        if url.endswith("/inquire-time-dailychartprice"):
            return Resp({"rt_cd": "0", "output2": minute_rows})
        assert url.endswith("/inquire-daily-itemchartprice")
        if provider_daily_error:
            return Resp({"rt_cd": "1", "msg_cd": provider_daily_error,
                         "msg1": "SENSITIVE_KIS_PROVIDER_MESSAGE"})
        return Resp({"rt_cd": "0", "output2": daily_rows or []})
    return get, calls


@pytest.mark.parametrize("market,labels,expected_phase", [
    ("J", ["090000", "152000", "153000", "160000", "195900"],
     "krx_after_market_candidate"),
    ("NX", ["080000", "085000", "090000", "154000", "195900"],
     "nxt_after_market_candidate"),
    ("UN", ["080000", "090000", "153000", "160000", "195900"],
     "integrated_after_1530_unknown_venue"),
])
def test_market_snapshots_separately_persisted_with_unverified_rules(
    tmp_path, market, labels, expected_phase,
):
    getter, calls = responder(
        [bar(x) for x in labels],
        [dict(bar("153000"), stck_clpr="102", acml_vol="555")],
    )
    result = capture(
        symbol=TICKER, session=SESSION, market=market, out_dir=tmp_path,
        key="DO_NOT_PRINT_KEY", secret="DO_NOT_PRINT_SECRET",
        token="DO_NOT_PRINT_BEARER", getter=getter, now=NOW, pause=lambda _: None,
    )
    path = Path(result["snapshot"])
    assert path.name == f"KIS_{market}_EXT_{TICKER}_{SESSION}"
    audit = json.loads((path/"audit.json").read_text())
    assert audit["market"] == market
    assert audit["venue"] == MARKETS[market]
    assert audit["unique_minute_labels"] == len(labels)
    assert audit["clock_phase_counts_not_session_completeness"][expected_phase] >= 1
    assert audit["requested_end_time"] == "200000"
    assert audit["automatic_5min_conversion"] is False
    assert audit["usable_for_backtesting"] is False
    assert audit["orders_enabled"] is False
    assert audit["daily_comparison"]["requested_date_matches"] == 1
    assert audit["daily_comparison"]["usable_for_backtesting"] is False
    assert (path/"raw"/"minute_000.json").is_file()
    assert (path/"raw"/"daily.json").is_file()
    assert sha256((path/"raw"/"daily.json").read_bytes()).hexdigest() == (
        audit["daily_comparison"]["raw_sha256"])
    minute_request = calls[0][1]
    assert minute_request["params"]["FID_COND_MRKT_DIV_CODE"] == market
    assert minute_request["params"]["FID_INPUT_HOUR_1"] == "200000"
    assert minute_request["params"]["FID_PW_DATA_INCU_YN"] == "N"
    assert calls[1][1]["params"]["FID_COND_MRKT_DIV_CODE"] == market
    assert calls[1][1]["params"]["FID_ORG_ADJ_PRC"] == "1"
    assert len(calls) == 2
    assert "DO_NOT_PRINT" not in (path/"audit.json").read_text()
    assert "DO_NOT_PRINT" not in (path/"minute_observations_UNVERIFIED.json").read_text()
    assert not (tmp_path/"orders.db").exists()
    with pytest.raises(VenueProbeError, match="already exists"):
        capture(symbol=TICKER,session=SESSION,market=market,out_dir=tmp_path,
                key="KEY",secret="SECRET",token="TOKEN",getter=getter,now=NOW)


def test_J_NX_UN_with_identical_time_remain_independent(tmp_path):
    for market, price in [("J", "100"), ("NX", "110"), ("UN", "105")]:
        getter, calls = responder([bar("155900",price=price)])
        capture(symbol=TICKER, session=SESSION, market=market, out_dir=tmp_path,
                key="K",secret="S",token="T",getter=getter,now=NOW)
    paths = sorted(tmp_path.glob("KIS_*"))
    assert len(paths) == 3
    values = {}
    for folder in paths:
        audit = json.loads((folder/"audit.json").read_text())
        rows = json.loads((folder/"minute_observations_UNVERIFIED.json").read_text())
        values[audit["market"]] = rows[0]["stck_prpr"]
        assert audit["integrated_disjoint_sum_of_venues"] == "UNVERIFIED"
    assert values == {"J":"100","NX":"110","UN":"105"}


def test_daily_failure_cannot_abort_successful_market_minute_snapshot(tmp_path):
    getter, _ = responder([bar("154000")], provider_daily_error="EGW00123")
    result=capture(symbol=TICKER,session=SESSION,market="NX",out_dir=tmp_path,
                   key="K",secret="S",token="T",getter=getter,now=NOW)
    path=Path(result["snapshot"])
    audit=json.loads((path/"audit.json").read_text())
    assert audit["daily_comparison"]["status"] == "DAILY_NOT_COLLECTED"
    assert audit["daily_comparison"]["safe_provider_code"] == "EGW00123"
    assert "SENSITIVE" not in (path/"audit.json").read_text()
    assert not (path/"raw"/"daily.json").exists()
    assert (path/"raw"/"minute_000.json").exists()


def test_cross_session_previous_day_ignored_and_future_day_blocked():
    for market in MARKETS:
        getter, _ = responder([bar("200000"), bar("194500", day="20261022")])
        pages, rows, audit=collect_minute(symbol=TICKER,session=SESSION,market=market,
                                          key="K",secret="S",token="T",
                                          getter=getter,now=NOW,pause=lambda _:None)
        assert len(pages)==1
        assert len(rows)==1
        assert audit["excluded_previous_session_rows"]==1
        assert audit["pagination_stop"]=="prior_day_boundary"
    getter, _ = responder([bar("200000",day="20261024")])
    with pytest.raises(VenueProbeError,match="Future"):
        collect_minute(symbol=TICKER,session=SESSION,market="J",
                       key="K",secret="S",token="T",getter=getter,now=NOW)


def test_failure_on_missing_market_data_invalid_codes_and_untrusted_provider_codes(tmp_path):
    getter,_=responder([])
    with pytest.raises(VenueProbeError,match="No matching"):
        collect_minute(symbol=TICKER,session=SESSION,market="NX",
                       key="K",secret="S",token="T",getter=getter,now=NOW)
    with pytest.raises(VenueProbeError,match="market"):
        capture(symbol=TICKER,session=SESSION,market="../UN",out_dir=tmp_path,
                key="K",secret="S",token="T",getter=getter,now=NOW)
    class Rejected:
        content=json.dumps({"rt_cd":"1","msg_cd":"TOKEN_LEAK: unsafe",
                            "msg1":"SECRET_DO_NOT_LOG"}).encode()
        def raise_for_status(self):
            return None
    with pytest.raises(VenueProbeError,match="msg_cd=UNKNOWN") as exc:
        collect_minute(symbol=TICKER,session=SESSION,market="UN",
                       key="K",secret="S",token="T",now=NOW,
                       getter=lambda *a,**kw:Rejected())
    assert "SECRET" not in str(exc.value)


def test_clock_phase_is_never_execution_venue_proof():
    assert phase("J", "153000") == "krx_closing_auction_marker"
    assert phase("NX", "153000") == "nxt_other_or_break"
    assert phase("NX", "080000") == "nxt_pre_market"
    assert phase("NX", "090000") == "nxt_main_market_candidate"
    assert phase("UN", "153000") == "integrated_0900_1530_unknown_venue"

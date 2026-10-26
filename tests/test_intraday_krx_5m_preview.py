"""Historical 1m-to-5m preview tests: independent source hashes, no PnL."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_krx_5m_preview import CLOCKS, build, candidate
from quantlab.intraday_kis_venues import capture, VenueProbeError

SYMS=("005930","000660")
DAYS=("2026-10-21","2026-10-22","2026-10-23")
NOW=datetime(2026,10,26,4,tzinfo=timezone.utc)


class Response:
    def __init__(self, value):
        self.content=json.dumps(value).encode()
    def raise_for_status(self):
        return None


def make_source(root: Path, *, symbol="005930", session="2026-10-23",
                omit=None, adjustments=None):
    adjustments=adjustments or {}
    day=session.replace("-","")
    minute=[]
    for ix,t in enumerate(CLOCKS):
        if t==omit:
            continue
        minute.append({
            "stck_bsop_date":day,"stck_cntg_hour":t,
            "stck_oprc":"100","stck_hgpr":"101",
            "stck_lwpr":"99","stck_prpr":"100",
            "cntg_vol":str(200+adjustments.get(t,0)),
        })
    minute.append({
        "stck_bsop_date":day, "stck_cntg_hour":"153000",
        "stck_oprc":"100","stck_hgpr":"101",
        "stck_lwpr":"99","stck_prpr":"100","cntg_vol":"400",
    })
    def get(url, **kw):
        if url.endswith("inquire-daily-itemchartprice"):
            return Response({"rt_cd":"0","output2":[{
                "stck_bsop_date":day,
                "acml_vol":str(380*200+400+1000),
                "stck_clpr":"100",
            }]})
        cursor=kw["params"]["FID_INPUT_HOUR_1"]
        newer=[x for x in minute if x["stck_cntg_hour"]<cursor]
        newer.sort(key=lambda v:v["stck_cntg_hour"],reverse=True)
        return Response({"rt_cd":"0","output2":newer[:120]})
    capture(symbol=symbol,session=session,market="J",out_dir=root,
            key="FAKE_KEY",secret="FAKE_SECRET",token="FAKE_TOKEN",
            getter=get,now=NOW,pause=lambda _:None)


def test_complete_multi_stock_multi_session_76_bar_sources_and_gate(tmp_path):
    source=tmp_path/"new"
    old=tmp_path/"previous"
    for symbol in SYMS:
        for session in DAYS:
            make_source(source,symbol=symbol,session=session)
    make_source(old,adjustments={"090700":69,"090800":-69})
    result=build(source_root=source,baseline_root=old,
                 symbols=SYMS,sessions=DAYS,out_dir=tmp_path/"report")
    s=result["summary"]
    assert s["complete_symbol_session_cases"]==6
    assert s["total_candidate_regular_5m_candles"]==456
    assert s["independent_baseline_case_count"]==1
    assert s["independent_5m_revision_case_count"]==0
    row=next(v for v in s["case_results"]
             if v["session"]=="2026-10-23" and v["symbol"]=="005930")
    assert row["candidate_5m_labels"]==76
    assert row["source_minute_labels"]==380
    assert row["closing_auction_153000_excluded"] is True
    assert row["official_daily_minus_regular_volume"]==1400
    assert row["independent_replay_comparison"]["minute_source_sha256_equal"] is False
    assert row["independent_replay_comparison"]["all_76_5m_ohlcv_identical"] is True
    assert row["independent_replay_comparison"]["changed_5m_bar_count"]==0
    assert s["strategy_selection_performed"] is False
    assert s["walkforward_return_calculated"] is False
    assert s["usable_for_backtesting"] is False
    for symbol in SYMS:
        p=tmp_path/"report"/f"{symbol}_candidate_5m_UNVERIFIED.json"
        bars=json.loads(p.read_bytes())
        assert len(bars)==len(DAYS)*76
        assert bars[0]["timestamp"].endswith("09:00:00+09:00")
        assert bars[75]["timestamp"].endswith("15:15:00+09:00")
        assert bars[1]["volume"]==1000
        assert sha256(p.read_bytes()).hexdigest()==p.with_suffix(".sha256").read_text().strip()
        assert s["readiness_by_symbol"][symbol]["sessions_missing_to_minimum"]==57
        assert s["readiness_by_symbol"][symbol]["eligible_to_call_intraday_research_evaluate"] is False
    q=Path(result["output"])
    assert sha256(q.read_bytes()).hexdigest()==q.with_suffix(".sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already exists"):
        build(source_root=source,symbols=SYMS,sessions=DAYS,out_dir=tmp_path/"report")


def test_5m_replay_changes_that_cross_bucket_boundary_are_not_hidden(tmp_path):
    source=tmp_path/"new";old=tmp_path/"old"
    make_source(source)
    make_source(old,adjustments={"090900":69,"091000":-69})
    s=build(source_root=source,baseline_root=old,symbols=("005930",),
            sessions=("2026-10-23",),out_dir=tmp_path/"report")["summary"]
    r=s["case_results"][0]["independent_replay_comparison"]
    assert r["minute_source_sha256_equal"] is False
    assert r["all_76_5m_ohlcv_identical"] is False
    assert r["changed_5m_bar_count"]==2
    assert s["independent_5m_revision_case_count"]==1
    assert s["usable_for_paper_or_live"] is False


def test_missing_minute_rejected_instead_of_synthetic_5m(tmp_path):
    source=tmp_path/"new"
    make_source(source,omit="091100")
    with pytest.raises(VenueProbeError,match="incomplete"):
        build(source_root=source,symbols=("005930",),
              sessions=("2026-10-23",),out_dir=tmp_path/"report")
    assert not (tmp_path/"report"/"candidate_5m_summary.json").exists()


def test_no_cross_market_fill_or_silent_missing_day(tmp_path):
    source=tmp_path/"new"
    make_source(source)
    with pytest.raises(VenueProbeError,match="snapshot missing"):
        build(source_root=source,symbols=SYMS,sessions=("2026-10-23",),
              out_dir=tmp_path/"out")
    assert not (tmp_path/"out"/"candidate_5m_summary.json").exists()


def test_tampered_source_refused(tmp_path):
    source=tmp_path/"new"
    make_source(source)
    file=source/"KIS_J_EXT_005930_2026-10-23"/"raw"/"minute_000.json"
    file.write_bytes(file.read_bytes()+b"ALTERED")
    with pytest.raises(VenueProbeError,match="hash"):
        candidate(source,symbol="005930",session="2026-10-23")


@pytest.mark.parametrize("symbols,sessions",[
    (("../unsafe",),("2026-10-23",)),
    (("005930",),("2026-10-24",)),
    (("005930","005930"),("2026-10-23",)),
])
def test_input_plan_rejects_unsafe_dates_and_symbols(tmp_path,symbols,sessions):
    with pytest.raises(VenueProbeError):
        build(source_root=tmp_path/"source",symbols=symbols,sessions=sessions,
              out_dir=tmp_path/"out")
    assert not (tmp_path/"out").exists()


def test_future_strategy_engine_rejects_three_unapproved_sessions(tmp_path):
    import pandas as pd
    from quantlab.intraday_research import validate, IntradayDataError

    source=tmp_path/"source"
    for day in DAYS:
        make_source(source,session=day)
    result=build(source_root=source,symbols=("005930",),sessions=DAYS,
                 out_dir=tmp_path/"research")["summary"]
    assert result["walkforward_return_calculated"] is False
    # Existing independent strategy engine rejects even a complete 3-day
    # candidate; converted bars are *not* silently auto-promoted.
    path=tmp_path/"research"/"005930_candidate_5m_UNVERIFIED.json"
    raw=json.loads(path.read_bytes())
    with pytest.raises(IntradayDataError,match="At least 60"):
        validate(pd.DataFrame(raw),"KRX_STOCK")


def test_chronologically_reversed_sessions_are_rejected(tmp_path):
    with pytest.raises(VenueProbeError,match="chronological"):
        build(source_root=tmp_path/"source",symbols=("005930",),
              sessions=("2026-10-23","2026-10-22"),out_dir=tmp_path/"out")
    assert not (tmp_path/"out").exists()

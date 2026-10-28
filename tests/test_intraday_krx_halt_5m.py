"""KRX circuit-breaker-aware 5m candidate slots are nontradable research."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

import quantlab.intraday_krx_halt_5m as mod
from quantlab.intraday_krx_5m_preview import CLOCKS
from quantlab.intraday_kis_venues import VenueProbeError, capture

NOW = datetime(2026,10,26,4,tzinfo=timezone.utc)
TICKER = "005930"


def clock_slice(start: str, stop: str) -> set[str]:
    return {x for x in CLOCKS if start <= x <= stop}


def mock_rows(*, session: str, missing: set[str] | None = None,
              closing: str = "153000", bad_vol: bool = False):
    missing = missing or set()
    rows={}
    for t in CLOCKS:
        if t in missing:
            continue
        rows[t]={
            "stck_bsop_date": session.replace("-", ""),
            "stck_cntg_hour": t,
            "stck_oprc": "100", "stck_hgpr": "101",
            "stck_lwpr": "99", "stck_prpr": "100",
            "cntg_vol": "-100" if bad_vol and t=="091000" else "100",
        }
    rows[closing]={
        "stck_bsop_date": session.replace("-", ""),
        "stck_cntg_hour": closing,
        "stck_oprc": "100", "stck_hgpr": "101",
        "stck_lwpr": "99", "stck_prpr": "100", "cntg_vol": "400",
    }
    return rows


def inject(monkeypatch, root: Path, fixtures: dict):
    # Fixture contents represent an independent _read_one whose raw hashes
    # have already been checked; this unit test isolates halt-state behavior.
    def stub(source, symbol, day, market):
        assert market == "J"
        assert symbol == TICKER
        assert source == root
        assert (source / f"KIS_J_EXT_{symbol}_{day}").is_dir()
        return {"source_minute_bar_sha256": "a"*64}, fixtures[day]
    monkeypatch.setattr(mod, "_read_one", stub)
    for day in fixtures:
        (root / f"KIS_J_EXT_{TICKER}_{day}").mkdir(parents=True)


def test_28_july_circuit_breaker_gaps_no_imputation_or_execution(tmp_path, monkeypatch):
    day="2026-07-28"
    missing=clock_slice("101400","104300")
    source=tmp_path/"sources"
    inject(monkeypatch, source,{day:mock_rows(session=day,missing=missing)})
    r=mod.analyze_day(source_root=source,symbol=TICKER,session=day)
    assert r["missing_regular_minute_count"]==30
    assert r["complete_regular_session_clock"] is False
    assert r["reported_cb"]["activation_kst"].endswith("10:13:43+09:00")
    assert r["reported_cb"]["20min_halt_plus_10min_call_auction_is_provisional"]
    assert r["day_disposition"]=="ENTIRE_DAY_STRATEGY_PNL_BLOCKED_MARKET_HALT_RISK"
    assert r["incomplete_five_minute_bucket_count"] == 7
    assert len(r["slots"])==76
    for slot in r["slots"]:
        assert slot["continuous_order_execution_allowed"] is False
        assert slot["is_complete_and_verified_for_strategy"] is False
        if slot["missing_one_minute_clock_labels"]:
            assert slot["ohlcv"] is None
            assert slot["state"].startswith("INCOMPLETE_")
        else:
            assert slot["ohlcv"]["volume"]==500
    # 1m source is conserved only across genuinely complete buckets;
    # partial source minutes are not fabricated into a 5m candle.
    assert (r["fully_observed_5m_bucket_volume_sum"] +
            r["remainder_is_observed_minutes_in_incomplete_buckets_not_imputed"]
            == r["observed_regular_minute_volume_sum"])


def test_isolated_29_july_missing_label_not_mislabeled_halt(tmp_path,monkeypatch):
    day="2026-07-29"
    missing=clock_slice("123300","130100")|{"120200"}
    root=tmp_path/"data"
    inject(monkeypatch,root,{day:mock_rows(session=day,missing=missing)})
    x=mod.analyze_day(source_root=root,symbol=TICKER,session=day)
    assert x["missing_regular_minute_count"]==30
    separate=next(s for s in x["slots"] if s["timestamp"].endswith("12:00:00+09:00"))
    assert separate["state"]=="INCOMPLETE_SOURCE_GAP_UNEXPLAINED"
    assert separate["ohlcv"] is None
    assert any(s["state"]=="INCOMPLETE_CB_CORRELATED_UNVERIFIED"
               for s in x["slots"])
    assert x["day_disposition"].endswith("MARKET_HALT_RISK")


def test_15_oct_reports_blackout_and_never_fakes_13h_candles(tmp_path,monkeypatch):
    day="2026-10-15"
    root=tmp_path/"data"
    inject(monkeypatch,root,{
        day:mock_rows(session=day,missing=clock_slice("123900","130700"))
    })
    x=mod.analyze_day(source_root=root,symbol=TICKER,session=day)
    assert x["missing_regular_minute_count"]==29
    assert x["reported_cb"]["activation_kst"].endswith("12:38:12+09:00")
    window=next(s for s in x["slots"] if s["timestamp"].endswith("13:05:00+09:00"))
    assert window["market_reported_cb_window_overlap"] is True
    assert window["ohlcv"] is None
    assert window["continuous_order_execution_allowed"] is False
    assert x["day_disposition"]=="ENTIRE_DAY_STRATEGY_PNL_BLOCKED_MARKET_HALT_RISK"


def test_unexplained_open_missing_and_nonstandard_close_are_independent(tmp_path,monkeypatch):
    source=tmp_path/"data"
    bad_open="2026-07-31"
    unusual_close="2026-08-13"
    inject(monkeypatch,source,{
        bad_open:mock_rows(session=bad_open,missing={"090000","090100"}),
        unusual_close:mock_rows(session=unusual_close,closing="153200"),
    })
    a=mod.analyze_day(source_root=source,symbol=TICKER,session=bad_open)
    assert a["reported_cb"] is None
    assert a["day_disposition"]=="ENTIRE_DAY_STRATEGY_PNL_BLOCKED_UNKNOWN_GAPS"
    assert a["slots"][0]["ohlcv"] is None
    assert a["slots"][0]["state"]=="INCOMPLETE_SOURCE_GAP_UNEXPLAINED"
    b=mod.analyze_day(source_root=source,symbol=TICKER,session=unusual_close)
    assert b["complete_regular_session_clock"] is True
    assert b["closing_153000_label_present"] is False
    assert b["observed_closing_clock_candidates"]==["153200"]
    assert b["day_disposition"]=="ENTIRE_DAY_STRATEGY_PNL_BLOCKED_END_MARKER_UNVERIFIED"
    assert all(v["ohlcv"] is not None for v in b["slots"])


def test_complete_day_76_bars_remains_vendortime_hold(tmp_path,monkeypatch):
    day="2026-10-23";root=tmp_path/"data"
    inject(monkeypatch,root,{day:mock_rows(session=day)})
    result=mod.analyze_day(source_root=root,symbol=TICKER,session=day)
    assert result["complete_five_minute_bucket_count"]==76
    assert result["observed_regular_minute_volume_sum"]==38000
    assert result["fully_observed_5m_bucket_volume_sum"]==38000
    assert result["day_disposition"]=="PROVISIONAL_COMPLETE_DAY_VENDOR_SEMANTICS_HOLD"
    assert all(s["ohlcv"]["volume"]==500 for s in result["slots"])
    assert all(not s["continuous_order_execution_allowed"] for s in result["slots"])


def test_invalid_minute_data_fails_closed_even_in_partial_bucket(tmp_path,monkeypatch):
    day="2026-10-23";root=tmp_path/"data"
    inject(monkeypatch,root,{day:mock_rows(session=day,missing={"091100"},bad_vol=True)})
    with pytest.raises(VenueProbeError,match="Contradictory"):
        mod.analyze_day(source_root=root,symbol=TICKER,session=day)


def test_immutable_output_and_day_risk_provenance(tmp_path,monkeypatch):
    root=tmp_path/"source"; out=tmp_path/"output"
    days=("2026-07-28","2026-10-23")
    inject(monkeypatch,root,{
        "2026-07-28":mock_rows(session="2026-07-28",
                                   missing=clock_slice("101400","104300")),
        "2026-10-23":mock_rows(session="2026-10-23"),
    })
    result=mod.build(source_root=root,out_dir=out,symbols=(TICKER,),sessions=days)
    s=result["summary"]
    assert s["total_stock_days"]==2
    assert s["total_5m_clock_slots"]==152
    assert s["halt_days_not_silently_dropped_from_dataset"] is True
    assert s["walkforward_returns_calculated"] is False
    assert s["usable_for_backtesting"] is False
    path=out/f"{TICKER}_halt_5m_UNVERIFIED.json"
    stored=json.loads(path.read_bytes())
    assert len(stored)==2
    assert sum(len(x["slots"]) for x in stored)==152
    for f in (path,out/"halt_5m_summary.json"):
        assert sha256(f.read_bytes()).hexdigest()==f.with_suffix(".sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already exists"):
        mod.build(source_root=root,out_dir=out,symbols=(TICKER,),sessions=days)


def test_invalid_session_order_and_missing_source_fails_without_outputs(tmp_path):
    with pytest.raises(VenueProbeError,match="chronological"):
        mod.build(source_root=tmp_path/"source",out_dir=tmp_path/"out",
                  symbols=(TICKER,),sessions=("2026-10-23","2026-10-22"))
    with pytest.raises(VenueProbeError,match="source missing"):
        mod.build(source_root=tmp_path/"source",out_dir=tmp_path/"out",
                  symbols=(TICKER,),sessions=("2026-10-23",))
    assert not (tmp_path/"out").exists()


def test_real_vendor_source_hash_tamper_blocks_analysis(tmp_path):
    day="2026-10-23"
    def getter(url, **kw):
        if url.endswith("inquire-daily-itemchartprice"):
            return _response({"rt_cd":"0","output2":[{
                "stck_bsop_date":"20261023","stck_clpr":"100",
                "acml_vol":"38000"
            }]})
        cursor=kw["params"]["FID_INPUT_HOUR_1"]
        rows=list(mock_rows(session=day).values())
        rows=[r for r in rows if r["stck_cntg_hour"]<cursor]
        rows.sort(key=lambda r:r["stck_cntg_hour"],reverse=True)
        return _response({"rt_cd":"0","output2":rows[:120]})
    root=tmp_path/"source"
    capture(symbol=TICKER,session=day,market="J",
            out_dir=root,key="DUMMY_KEY",secret="DUMMY_SECRET",
            token="DUMMY_TOKEN",getter=getter,now=NOW,pause=lambda _:None)
    f=root/f"KIS_J_EXT_{TICKER}_{day}"/"raw"/"minute_000.json"
    f.write_bytes(f.read_bytes()+b"TAMPERED")
    with pytest.raises(VenueProbeError):
        mod.analyze_day(source_root=root,symbol=TICKER,session=day)


class _response:
    def __init__(self,body):
        self.content=json.dumps(body).encode()
    def raise_for_status(self):
        return None


def test_minute_before_reported_activation_is_not_attributed_to_breaker(
    tmp_path, monkeypatch
):
    day = "2026-07-28"
    root = tmp_path / "source"
    inject(monkeypatch, root, {
        day: mock_rows(session=day, missing={"101100", "101400"})
    })
    r = mod.analyze_day(source_root=root, symbol=TICKER, session=day)
    slot = next(x for x in r["slots"] if x["timestamp"].endswith("10:10:00+09:00"))
    assert slot["market_reported_cb_window_overlap"] is True
    # 10:11 minute interval precedes 10:13:43 breaker activation.
    assert slot["state"] == "INCOMPLETE_CB_AND_UNEXPLAINED_GAPS"
    assert slot["ohlcv"] is None
    assert r["day_disposition"] == "ENTIRE_DAY_STRATEGY_PNL_BLOCKED_MARKET_HALT_RISK"


def test_readiness_never_removes_breaker_days_to_claim_walkforward(
    tmp_path, monkeypatch
):
    root=tmp_path/"source"
    days=("2026-07-28","2026-10-23")
    inject(monkeypatch,root,{
        days[0]:mock_rows(session=days[0],missing=clock_slice("101400","104300")),
        days[1]:mock_rows(session=days[1]),
    })
    result=mod.build(source_root=root,out_dir=tmp_path/"result",
                     symbols=(TICKER,),sessions=days)["summary"]
    readiness=result["readiness_by_symbol"][TICKER]
    assert readiness["market_halt_days_preserved"]==1
    assert readiness["clean_complete_session_candidates"]==1
    assert readiness["clean_session_shortfall_to_60"]==59
    assert readiness["must_not_drop_halt_days_to_inflate_test_performance"] is True
    assert readiness["legacy_strategy_evaluate_permitted"] is False

@pytest.mark.parametrize("day,activation", [
    ("2026-06-08","09:03:42"),
    ("2026-06-23","14:33:43"),
    ("2026-06-26","12:10:12"),
    ("2026-07-07","13:51:34"),
    ("2026-07-13","13:28:32"),
])
def test_previously_missing_reported_market_cb_is_hard_session_gate(
    day,activation,tmp_path,monkeypatch
):
    root=tmp_path/"raw"
    inject(monkeypatch,root,{day:mock_rows(session=day)})
    result=mod.analyze_day(source_root=root,symbol=TICKER,session=day)
    assert result["reported_cb"]["activation_kst"].endswith(activation+"+09:00")
    assert result["day_disposition"]=="ENTIRE_DAY_STRATEGY_PNL_BLOCKED_MARKET_HALT_RISK"
    assert result["backtest_pnl_enabled"] is False
    assert all(not row["continuous_order_execution_allowed"]
               for row in result["slots"])

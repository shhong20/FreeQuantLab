"""Small, completely mocked KIS multi-day capture/cohort regression gate."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_kis_cohort_capture import (
    DEFAULT_DATES, DEFAULT_MARKETS, DEFAULT_SYMBOLS,
    PacedGetter, plan, run,
)
from quantlab.intraday_kis_cohort_audit import cohort
from quantlab.intraday_kis_venues import VenueProbeError, capture


class VendorResponse:
    def __init__(self, data):
        self.content = json.dumps(data).encode()

    def raise_for_status(self):
        return None


def vendor_get(*, mismatch=False, deny_market=None):
    calls = []
    def get(url, **kwargs):
        params = kwargs["params"]
        market = params["FID_COND_MRKT_DIV_CODE"]
        calls.append((market, url))
        if deny_market == market:
            return VendorResponse({
                "rt_cd": "1", "msg_cd": "EGW00201",
                "msg1": "DO_NOT_LOG_ACCOUNT_OR_TOKEN",
            })
        day = params.get("FID_INPUT_DATE_1", "20261023")
        if url.endswith("inquire-daily-itemchartprice"):
            vol = {"J":100, "NX":200, "UN":300}[market]
            return VendorResponse({
                "rt_cd":"0", "output2":[{
                    "stck_bsop_date": day, "acml_vol": str(vol),
                    "stck_clpr":"100",
                }],
            })
        assert url.endswith("inquire-time-dailychartprice")
        quantity = {"J":100,"NX":200,"UN":300}[market]
        if mismatch and market=="UN":
            quantity=299
        return VendorResponse({
            "rt_cd":"0", "output2":[{
                "stck_bsop_date":day, "stck_cntg_hour":"090000",
                "stck_oprc":"100", "stck_hgpr":"100",
                "stck_lwpr":"100", "stck_prpr":"100",
                "cntg_vol":str(quantity),
            }],
        })
    return get, calls


def make_data(root, *, symbols=("005930",), sessions=("2026-10-23",),
              markets=("J","NX","UN"), mismatch=False):
    getter, calls=vendor_get(mismatch=mismatch)
    for session in sessions:
        for symbol in symbols:
            for market in markets:
                capture(symbol=symbol,session=session,market=market,out_dir=root,
                        key="DUMMY_KEY_NOT_FOR_LOG",secret="DUMMY_SECRET_NOT_FOR_LOG",
                        token="DUMMY_TOKEN_NOT_FOR_LOG",getter=getter)
    return calls


def test_plan_small_fixed_sessions_and_bounds():
    tasks=plan(symbols=DEFAULT_SYMBOLS,sessions=DEFAULT_DATES,
               markets=DEFAULT_MARKETS,today=date(2026,10,26))
    assert len(tasks)==18
    assert tasks[0] == {"symbol":"005930", "session":"2026-10-21", "market":"J"}
    assert tasks[-1] == {"symbol":"000660", "session":"2026-10-23", "market":"UN"}
    with pytest.raises(VenueProbeError,match="weekdays"):
        plan(symbols="005930",sessions="2026-10-24",markets="J",today=date(2026,10,26))
    with pytest.raises(VenueProbeError,match="past"):
        plan(symbols="005930",sessions="2026-10-26",markets="J",today=date(2026,10,26))
    with pytest.raises(VenueProbeError,match="unique"):
        plan(symbols="005930,005930",sessions="2026-10-23",markets="J",
             today=date(2026,10,26))
    with pytest.raises(VenueProbeError,match="30"):
        plan(symbols="005930,000660,035420",sessions="2026-10-19,2026-10-20,2026-10-21,2026-10-22,2026-10-23",
             markets="J,NX,UN",today=date(2026,10,26))


def test_dry_run_makes_no_api_requests_or_file_writes(tmp_path):
    tasks=plan(symbols="005930",sessions="2026-10-23",markets="J,NX,UN",
               today=date(2026,10,26))
    out=run(tasks=tasks,root=tmp_path/"run",execute=False,
            getter=lambda *a,**kw:pytest.fail("network in dry run"))
    assert out["status"]=="DRY_RUN"
    assert len(out["tasks"])==3
    assert all(x["status"]=="NOT_COLLECTED" for x in out["tasks"])
    assert out["usable_for_backtesting"] is False
    assert not (tmp_path/"run").exists()


def test_capture_paced_and_resume_verified_without_extra_calls(tmp_path):
    class Clock:
        current=0.0
        def clock(self):
            return self.current
        def sleep(self, seconds):
            self.current += seconds
    fake=Clock()
    getter,calls=vendor_get()
    tasks=plan(symbols="005930",sessions="2026-10-23",
               markets="J,NX,UN",today=date(2026,10,26))
    root=tmp_path/"venue_capture"
    out=run(tasks=tasks,root=root,execute=True,
            key="DUMMY_KEY",secret="DUMMY_SECRET",token="DUMMY_TOKEN",
            getter=getter,clock=fake.clock,sleep=fake.sleep,
            pacing_seconds=1.0,between_tasks_seconds=2.0)
    assert out["status"]=="CAPTURES_COMPLETED"
    assert out["completed_snapshot_count"]==3
    assert out["http_get_requests"]==6
    assert len(calls)==6
    assert fake.current>=5.0
    assert out["secret_values_written"] is False
    persisted=(root/"batch_progress.json").read_text()
    assert "DUMMY_SECRET" not in persisted
    assert "DUMMY_TOKEN" not in persisted
    assert all((root/f"KIS_{m}_EXT_005930_2026-10-23").is_dir()
               for m in ("J","NX","UN"))
    another=run(tasks=tasks,root=root,execute=False)
    assert all(x["status"]=="EXISTING_VERIFIED" for x in another["tasks"])
    resume=run(tasks=tasks,root=root,execute=True,
               key="DUMMY_KEY",secret="DUMMY_SECRET",token="DUMMY_TOKEN",
               getter=lambda *a,**kw:pytest.fail("resumed call despite verified data"))
    assert resume["http_get_requests"]==0
    assert resume["completed_snapshot_count"]==3


def test_api_denial_stops_safely_without_leaking_secret_or_overwriting(tmp_path):
    getter,calls=vendor_get(deny_market="NX")
    tasks=plan(symbols="005930",sessions="2026-10-23",
               markets="J,NX,UN",today=date(2026,10,26))
    root=tmp_path/"failed"
    out=run(tasks=tasks,root=root,execute=True,key="DUMMY_KEY",
            secret="DO_NOT_LOG_SECRET",token="DO_NOT_LOG_TOKEN",
            getter=getter,sleep=lambda _:None,pacing_seconds=0.5)
    assert out["status"]=="STOPPED_ON_FAILURE_LIMIT"
    assert out["tasks"][0]["status"]=="COLLECTED_VERIFIED"
    assert out["tasks"][1]["status"]=="FAILED_CLOSED"
    assert out["tasks"][1]["error"]["provider_code"]=="EGW00201"
    assert out["tasks"][2]["status"]=="NOT_COLLECTED"
    report=(root/"batch_progress.json").read_text()
    assert "DO_NOT_LOG" not in report
    assert (root/"KIS_J_EXT_005930_2026-10-23").is_dir()
    assert not (root/"KIS_NX_EXT_005930_2026-10-23").exists()


def test_tampered_resumable_source_is_never_overwritten(tmp_path):
    root=tmp_path/"tampered"
    make_data(root,markets=("J",))
    raw=root/"KIS_J_EXT_005930_2026-10-23"/"raw"/"minute_000.json"
    raw.write_bytes(raw.read_bytes()+b"tampered")
    tasks=plan(symbols="005930",sessions="2026-10-23",markets="J",
               today=date(2026,10,26))
    with pytest.raises(VenueProbeError,match="hash"):
        run(tasks=tasks,root=root,execute=False)


def test_offline_multisession_cohort_counts_exact_anomalies(tmp_path):
    root=tmp_path/"venue_sources"
    symbols=("005930","000660")
    dates=("2026-10-22","2026-10-23")
    make_data(root,symbols=symbols,sessions=dates,mismatch=True)
    out=cohort(source_root=root,symbols=symbols,sessions=dates,
               output=tmp_path/"cohort")
    summary=out["summary"]
    assert summary["requested_case_count"]==4
    assert summary["complete_three_market_case_count"]==4
    assert summary["incomplete_three_market_case_count"]==0
    assert summary["checked_integrated_minute_labels"]==4
    assert summary["minute_volume_mismatch_count"]==4
    assert summary["minute_price_envelope_mismatch_count"]==0
    assert summary["official_daily_volume_additivity_mismatch_case_count"]==0
    assert summary["minute_volume_mismatch_rate"]==1.0
    assert len(summary["case_results"])==4
    assert all(x["status"]=="MISMATCH_HOLD" for x in summary["case_results"])
    assert summary["usable_for_backtesting"] is False
    f=Path(out["output"])
    assert sha256(f.read_bytes()).hexdigest()==f.with_suffix(".sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already exists"):
        cohort(source_root=root,symbols=symbols,sessions=dates,
               output=tmp_path/"cohort")


def test_incomplete_market_never_counted_as_a_zero_error(tmp_path):
    root=tmp_path/"venue"
    make_data(root,markets=("J","NX"))
    output=cohort(source_root=root,symbols=("005930",),sessions=("2026-10-23",),
                  output=tmp_path/"cohort")
    s=output["summary"]
    assert s["status"]=="INSUFFICIENT_COMPLETE_CASES"
    assert s["complete_three_market_case_count"]==0
    assert s["incomplete_three_market_case_count"]==1
    assert s["checked_integrated_minute_labels"]==0
    assert s["minute_volume_mismatch_rate"] is None
    assert s["excluded_missing_cases"][0]["missing_markets"]==["UN"]

"""Offline KIS after-hours single-price market reform and source-scope tests."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_kis_overtime_regime import inspect
from quantlab.intraday_kis_overtime_daily import capture_one
from quantlab.intraday_kis_venues import capture, VenueProbeError

NOW=datetime(2026,10,26,4,tzinfo=timezone.utc)
SESSIONS=("2026-10-21","2026-10-22","2026-10-23")


class Resp:
    def __init__(self,obj):self.content=json.dumps(obj).encode()
    def raise_for_status(self):return None


def fixture(tmp_path,*,pre_nonzero_price=True,post_nonzero_price=False,
            volume_in_legacy=False):
    root=tmp_path/"overtime"
    cohort=tmp_path/"cohort"
    for symbol in ("005930","000660"):
        overtime_rows=[]
        for day in ("20260909","20260910","20260911",
                    "20260914","20261021","20261022","20261023"):
            before=day<"20260914"
            price = ("123" if pre_nonzero_price else "0") if before else (
                "123" if post_nonzero_price else "0"
            )
            overtime_rows.append({
                "stck_bsop_date":day,
                "ovtm_untp_vol":"3" if volume_in_legacy else "0",
                "ovtm_untp_prpr":price,
                "ovtm_untp_tr_pbmn":"0",
                "acml_vol":"1000",
                "stck_clpr":"100",
            })
        capture_one(
            symbol=symbol,sessions=SESSIONS,out_dir=root,
            key="FAKE_KEY",secret="FAKE_SECRET",token="FAKE_TOKEN",
            getter=lambda url,**kw:Resp({"rt_cd":"0","output2":overtime_rows}),
        )
        for session in SESSIONS:
            def get(url,**kw):
                if url.endswith("inquire-time-dailychartprice"):
                    return Resp({"rt_cd":"0","output2":[{
                        "stck_bsop_date":session.replace("-",""),
                        "stck_cntg_hour":"090000",
                        "stck_oprc":"100","stck_hgpr":"100",
                        "stck_lwpr":"100","stck_prpr":"100","cntg_vol":"900",
                    }]})
                return Resp({"rt_cd":"0","output2":[{
                    "stck_bsop_date":session.replace("-",""),
                    "acml_vol":"1000","stck_clpr":"100",
                }]})
            capture(symbol=symbol,session=session,market="J",out_dir=cohort,
                    key="FAKE_KEY",secret="FAKE_SECRET",token="FAKE_TOKEN",
                    getter=get,now=NOW)
    return root,cohort


def test_realistic_transition_zero_old_aftermarket_is_not_no_trading(tmp_path):
    root,cohort=fixture(tmp_path)
    res=inspect(overtime_root=root,cohort_root=cohort,
                symbols=("005930","000660"),sessions=SESSIONS,
                out_dir=tmp_path/"result")
    s=res["audit"]
    assert s["requested_case_count"]==6
    assert s["matched_case_count"]==6
    assert s["all_matched_daily_volume_parity"] is True
    assert s["all_matched_daily_close_parity"] is True
    assert s["field_scope_vendor_certified"] is False
    assert s["legacy_zero_does_not_reconcile_daily_minute_gaps"] is True
    assert s["usable_for_backtesting"] is False
    assert s["usable_for_paper_or_live"] is False
    assert all(t["before_transition"]["trading_day_rows"]==3 for t in s["source_timelines"])
    assert all(t["before_transition"]["fields"]["ovtm_untp_prpr"]["nonzero"]==3
               for t in s["source_timelines"])
    assert all(t["on_or_after_transition"]["fields"]["ovtm_untp_prpr"]["zero"]==4
               for t in s["source_timelines"])
    assert all(t["post_transition_overtime_single_price_price_all_zero"]
               for t in s["source_timelines"])
    assert all(t["legacy_volume_all_zero_even_before_transition"]
               for t in s["source_timelines"])
    p=Path(res["output"])
    assert sha256(p.read_bytes()).hexdigest() == (
        p.parent/"legacy_regime_scope.sha256").read_text().strip()
    assert "FAKE_SECRET" not in p.read_text()
    with pytest.raises(VenueProbeError,match="already exists"):
        inspect(overtime_root=root,cohort_root=cohort,
                symbols=("005930","000660"),sessions=SESSIONS,
                out_dir=tmp_path/"result")


def test_price_transition_not_assumed_if_post_date_has_price(tmp_path):
    root,cohort=fixture(tmp_path,post_nonzero_price=True)
    s=inspect(overtime_root=root,cohort_root=cohort,
              symbols=("005930",),sessions=SESSIONS,
              out_dir=tmp_path/"result")["audit"]
    assert s["source_timelines"][0]["post_transition_overtime_single_price_price_all_zero"] is False
    assert s["source_timelines"][0]["on_or_after_transition"]["fields"]["ovtm_untp_prpr"]["nonzero"]==4
    assert s["legacy_zero_does_not_reconcile_daily_minute_gaps"] is True


def test_legacy_volume_nonzero_does_not_authorize_market_repair(tmp_path):
    root,cohort=fixture(tmp_path,volume_in_legacy=True)
    s=inspect(overtime_root=root,cohort_root=cohort,
              symbols=("005930",),sessions=SESSIONS,
              out_dir=tmp_path/"result")["audit"]
    assert s["source_timelines"][0]["legacy_volume_all_zero_even_before_transition"] is False
    assert s["additional_out_of_session_volume_found"] is False
    assert s["no_synthetic_volume_correction"] is True


def test_raw_file_mutation_fails_before_scope_report_written(tmp_path):
    root,cohort=fixture(tmp_path)
    file=root/"KIS_OVERTIME_DAILY_J_005930"/"raw.json"
    file.write_bytes(file.read_bytes()+b"MUTATED")
    with pytest.raises(VenueProbeError):
        inspect(overtime_root=root,cohort_root=cohort,
                symbols=("005930",),sessions=SESSIONS,
                out_dir=tmp_path/"result")
    assert not (tmp_path/"result"/"legacy_regime_scope.json").exists()


def test_symbol_path_traversal_fails(tmp_path):
    with pytest.raises(VenueProbeError):
        inspect(overtime_root=tmp_path,cohort_root=tmp_path,
                symbols=("../../BAD",),sessions=SESSIONS,
                out_dir=tmp_path/"result")
    assert not (tmp_path/"result").exists()

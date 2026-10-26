"""Offline cohort source drift regression tests; no authenticated network calls."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_kis_cohort_revisions import survey
from quantlab.intraday_kis_venues import VenueProbeError, capture

NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
SYMBOL = "005930"


class Response:
    def __init__(self, obj):
        self.content = json.dumps(obj).encode()

    def raise_for_status(self):
        return None


def save_source(root: Path, *, market: str, session: str,
                revised: bool = False, header: str | None = None):
    first, second = {
        "J": (20, 30),
        "NX": (10, 20),
        "UN": (30, 50),
    }[market]
    if revised and market == "J":
        first -= 1
        second += 1
    if revised and market == "UN":
        second += 1

    def getter(url, **kwargs):
        day = kwargs["params"]["FID_INPUT_DATE_1"]
        if url.endswith("inquire-daily-itemchartprice"):
            return Response({
                "rt_cd":"0",
                "output1": {"stck_prpr": header or "100"},
                "output2": [{"stck_bsop_date":day, "acml_vol":"1000",
                             "stck_clpr":"100"}],
            })
        if not url.endswith("inquire-time-dailychartprice"):
            raise AssertionError("Unexpected endpoint")
        return Response({
            "rt_cd":"0",
            "output1":{"stck_prpr": header or "100"},
            "output2": [
                {"stck_bsop_date":day,"stck_cntg_hour":clock,
                 "stck_oprc":"100","stck_hgpr":"100",
                 "stck_lwpr":"100","stck_prpr":"100","cntg_vol":str(volume)}
                for clock,volume in (("090700",first),("090800",second))
            ],
        })
    capture(symbol=SYMBOL,session=session,market=market,out_dir=root,
            key="LOCAL_DUMMY",secret="LOCAL_DUMMY",token="LOCAL_DUMMY",
            getter=getter,now=NOW)


def make_three(root: Path, *, session="2026-10-23",
               revised=False, header=None):
    for market in ("J","NX","UN"):
        save_source(root,market=market,session=session,revised=revised,
                    header=header)


def test_detects_two_krx_and_one_integrated_historical_revisions(tmp_path):
    base,new=tmp_path/"baseline",tmp_path/"newer"
    make_three(base)
    make_three(new,revised=True)
    make_three(new,session="2026-10-22")
    path=tmp_path/"report"
    out=survey(baseline_root=base,newer_root=new,
               symbols=(SYMBOL,),sessions=("2026-10-22","2026-10-23"),
               out_dir=path)
    s=out["summary"]
    assert s["status"]=="HISTORICAL_SOURCE_REVISION_HOLD"
    assert s["requested_pairs"]==2
    assert s["complete_pairs"]==1
    assert s["missing_capture_pairs"]==1
    assert s["revision_affected_pairs"]==1
    assert s["changed_minute_labels_across_venues"]==3
    assert s["revision_affected_pair_rate"]==1
    assert s["excluded_pairs"][0]["baseline_missing"]==["J","NX","UN"]
    assert s["excluded_pairs"][0]["newer_missing"]==[]
    result=s["cases"][0]
    assert result["markets"]["J"]["changed_minute_labels"]==2
    assert result["markets"]["NX"]["changed_minute_labels"]==0
    assert result["markets"]["UN"]["changed_minute_labels"]==1
    assert result["markets"]["J"]["historical_raw_payload_status"] == (
        "HISTORICAL_PAGE_OR_DAILY_PAYLOAD_DIFFERENCE_HOLD")
    assert result["markets"]["UN"]["official_daily_historical_rows_identical"] is True
    assert result["passed_historical_source_stability_check"] is False
    assert s["usable_for_paper_or_live"] is False
    blob=(path/"cohort_revision_summary.json").read_bytes()
    assert sha256(blob).hexdigest() == (
        path/"cohort_revision_summary.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already exists"):
        survey(baseline_root=base,newer_root=new,symbols=(SYMBOL,),
               sessions=("2026-10-22","2026-10-23"),out_dir=path)


def test_only_quote_header_diff_is_not_historical_rewrite(tmp_path):
    base,new=tmp_path/"baseline",tmp_path/"newer"
    make_three(base,header="100")
    make_three(new,header="105")
    s=survey(baseline_root=base,newer_root=new,
             symbols=(SYMBOL,),sessions=("2026-10-23",),
             out_dir=tmp_path/"report")["summary"]
    assert s["status"]=="SOURCES_REPEATABLE_NOT_CERTIFIED"
    assert s["revision_affected_pairs"]==0
    assert s["changed_minute_labels_across_venues"]==0
    case=s["cases"][0]
    assert case["passed_historical_source_stability_check"] is True
    assert all(x["quote_header_changes_not_historical_revisions"]
               for x in case["markets"].values())
    assert s["usable_for_backtesting"] is False


def test_no_baseline_is_not_zero_revision_rate(tmp_path):
    base,new=tmp_path/"baseline",tmp_path/"newer"
    make_three(new)
    s=survey(baseline_root=base,newer_root=new,symbols=(SYMBOL,),
             sessions=("2026-10-23",),out_dir=tmp_path/"report")["summary"]
    assert s["status"]=="NO_COMPARABLE_REVISIONS"
    assert s["complete_pairs"]==0
    assert s["revision_affected_pair_rate"] is None
    assert s["missing_capture_pairs"]==1


def test_tampered_old_source_blocks_comparison(tmp_path):
    base,new=tmp_path/"baseline",tmp_path/"newer"
    make_three(base)
    make_three(new)
    f=base/f"KIS_J_EXT_{SYMBOL}_2026-10-23"/"raw"/"minute_000.json"
    f.write_bytes(f.read_bytes()+b"CORRUPT")
    with pytest.raises(VenueProbeError,match="hash"):
        survey(baseline_root=base,newer_root=new,symbols=(SYMBOL,),
               sessions=("2026-10-23",),out_dir=tmp_path/"report")
    assert not (tmp_path/"report"/"cohort_revision_summary.json").exists()


def test_invalid_date_and_same_source_roots_blocked(tmp_path):
    with pytest.raises(VenueProbeError):
        survey(baseline_root=tmp_path,newer_root=tmp_path,
               symbols=(SYMBOL,),sessions=("2026-10-23",),
               out_dir=tmp_path/"report")
    with pytest.raises(VenueProbeError):
        survey(baseline_root=tmp_path/"old",newer_root=tmp_path/"new",
               symbols=("../BAD",),sessions=("2026-10-23",),
               out_dir=tmp_path/"report")

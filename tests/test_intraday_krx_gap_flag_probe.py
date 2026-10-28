"""Bounded, quotation-only KIS fake-tick differential probe tests."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import pytest

import quantlab.intraday_krx_gap_flag_probe as mod
from quantlab.intraday_krx_gap_pair_audit import audit as audit_saved_pairs
from quantlab.intraday_kis_venues import VenueProbeError


def test_cursor_groups_nearby_minutes_without_exceeding_one_page_budget():
    assert mod._cursors(["090000", "090100"]) == ["094000"]
    assert mod._cursors(["101400", "143000"]) == ["105400", "151000"]
    assert mod._cursors(["150000"]) == ["153000"]


def test_sample_returns_clock_evidence_only_not_a_certified_trade():
    raw=json.dumps({"rt_cd":"0","output2":[{
        "stck_bsop_date":"20260601","stck_cntg_hour":"090000",
        "stck_oprc":"100","stck_hgpr":"101","stck_lwpr":"100",
        "stck_prpr":"101","cntg_vol":"0",
    }]}).encode()
    x=mod._sample(raw,symbol="005930",session="2026-06-01")
    assert x["clock_labels"]==["090000"]
    assert x["unverified_vendor_timestamp_semantics"] is True
    assert x["minute_source_sha256"]==sha256(raw).hexdigest()


def test_invalid_provider_response_fails_closed():
    raw=json.dumps({"rt_cd":"1","msg_cd":"INVALID","output2":[]}).encode()
    with pytest.raises(VenueProbeError,match="denied"):
        mod._sample(raw,symbol="005930",session="2026-06-01")


def test_mock_comparison_preserves_original_and_never_promotes_Y_mode(tmp_path,monkeypatch):
    gaps=[
        ["090000","090100"],["150000"],["090000","090100"],
        ["141800"],["101400","143000"],["090000","090100"],
        ["090000","090100","090300"],
    ]
    dates=["2026-06-01","2026-07-03","2026-07-31",
           "2026-07-08","2026-07-15","2026-06-25","2026-08-04"]
    assert sum(map(len,gaps))==13
    sources=[{"symbol": "005930" if i<3 else "000660",
              "session":d,"unknown_regular_minute_labels":clocks,
              "source_minute_sha256":"a"*64}
             for i,(d,clocks) in enumerate(zip(dates,gaps))]
    report={
        "status":"KRX_RAW_SOURCE_TRIAGE_HOLD_NO_PNL",
        "usable_for_backtesting":False,"usable_for_paper_or_live":False,
        "unexplained_cases":sources,
        "unexplained_missing_stock_days":7,
        "unexplained_missing_minute_labels":13,
    }
    triage=tmp_path/"triage"
    triage.mkdir()
    raw=(json.dumps(report,sort_keys=True)+"\n").encode()
    (triage/"source_triage.json").write_bytes(raw)
    original=tmp_path/"original"
    original.mkdir()
    def read_verified(root,name):
        assert root==triage
        return report
    def read_one(root,symbol,session,venue):
        assert venue=="J"
        return {"source_minute_bar_sha256":"a"*64},{}
    class FakeResponse:
        def __init__(self,data):self.content=data
        def raise_for_status(self):pass
    calls=[]
    def fake_get(url,params,headers,timeout):
        assert "quotations" in url
        assert headers["tr_id"]==mod.TR_ID
        assert params["FID_COND_MRKT_DIV_CODE"]=="J"
        mode=params["FID_FAKE_TICK_INCU_YN"]
        assert mode in ("","Y")
        calls.append((params["FID_INPUT_ISCD"],mode,params["FID_INPUT_HOUR_1"]))
        day=params["FID_INPUT_DATE_1"]
        target=next(t for t in sources if t["session"].replace("-","")==day)
        rows=[{"stck_bsop_date":day,"stck_cntg_hour":x,
               "stck_oprc":"100","stck_hgpr":"101","stck_lwpr":"99",
               "stck_prpr":"100","cntg_vol":"0"}
              for x in target["unknown_regular_minute_labels"]] if mode=="Y" else []
        return FakeResponse(json.dumps({"rt_cd":"0","output2":rows}).encode())
    monkeypatch.setattr(mod,"_read_verified",read_verified)
    monkeypatch.setattr(mod,"_read_one",read_one)
    out=tmp_path/"output"
    result=mod.compare(triage_dir=triage,original_root=original,
                       output_dir=out,key="redacted",secret="redacted",
                       token="redacted",getter=fake_get,pause=lambda _:None,
                       now=datetime(2026,10,28,tzinfo=timezone.utc))
    assert result["source_population"]==7
    assert result["missing_clocks_in_original"]==13
    assert len(calls)<=mod.MAX_QUERIES
    assert result["fake_tick_Y_only_total"]==13
    assert result["usable_for_backtesting"] is False
    assert result["raw_source_mutated"] is False
    assert result["Y_mode_not_evidence_of_real_trades"] is True
    assert sha256((out/"probe_report.json").read_bytes()).hexdigest()==(
        out/"probe_report.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already exists"):
        mod.compare(triage_dir=triage,original_root=original,
                    output_dir=out,key="redacted",secret="redacted",
                    token="redacted",getter=fake_get,pause=lambda _:None,
                    now=datetime(2026,10,28,tzinfo=timezone.utc))

    offline=audit_saved_pairs(probe_dir=out,output_dir=tmp_path/"paired")
    assert offline["query_mode_pairs"]==8
    assert offline["identical_clock_label_pairs"]==0
    assert offline["identical_all_minute_field_pairs"]==0
    # Mock Y-only data was synthetic: no trading conclusions allowed.
    assert offline["missing_original_clocks_restored"]==13
    assert offline["real_trade_or_fill_proven"] is False
    assert offline["usable_for_backtesting"] is False
    assert sha256((tmp_path/"paired"/"pair_audit.json").read_bytes()).hexdigest()==(
        tmp_path/"paired"/"pair_audit.sha256").read_text().strip()
    # Even a valid-looking edit to a stored page is rejected by its SHA.
    page=next(out.glob("J_*.json"))
    page.write_bytes(page.read_bytes()+b"TAMPER")
    with pytest.raises(VenueProbeError,match="checksum"):
        audit_saved_pairs(probe_dir=out,output_dir=tmp_path/"tampered")
    assert not (tmp_path/"tampered").exists()

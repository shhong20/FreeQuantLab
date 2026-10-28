"""One-day KRX pilot closeout: independent evidence and failure accounting."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import pytest

import quantlab.intraday_krx_joint_pilot_review as review
from quantlab.intraday_kis_venues import VenueProbeError

AFTER=datetime.fromisoformat("2026-10-29T16:00:00+09:00")


def save(folder:Path,stem:str,data:dict):
    folder.mkdir(parents=True,exist_ok=True)
    raw=(json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
    (folder/f"{stem}.json").write_bytes(raw)
    (folder/f"{stem}.sha256").write_text(sha256(raw).hexdigest()+"\n")
    return raw


def make_window(root:Path,window:str,*,date="2026-10-29",bad_symbol=False,
                audit=True,wrong_date=False):
    c=root/f"2026-10-29_{window}_raw"
    a=root/f"2026-10-29_{window}_audit"
    save(c,"manifest",{
        "status":"READ_ONLY_KRX_J_NEAR_TIME_L2_TICKS_1M_NO_FILLS",
        "rounds":2,"real_orders":0,"symbols":["005930","000660"],
        "usable_for_backtesting":False,
    })
    start={"morning":"09:43:00","midday":"12:35:00",
           "afternoon":"14:35:00"}[window]
    samples=[]
    for i in (0,1):
        for sy in ("005930","000660"):
            samples.append({
                "sample_index":i,
                "symbol":"000660" if bad_symbol else sy,
                "start_kst":f"{date}T{start}+09:00",
                "last_received_kst":f"{date}T{start[:6]}06+09:00",
                "receipt_span_seconds":6,
                "book_spread_bps":7.5 if sy=="005930" else 4.2,
            })
    report={
        "status":"KRX_NEAR_TIME_THREE_SOURCE_CONSISTENCY_NOT_FILL_VALIDATION",
        "sample_triplets":4,"broker_orders":0,
        "usable_for_backtesting":False,"usable_for_paper_or_live":False,
        "manifest_sha256":sha256((c/"manifest.json").read_bytes()).hexdigest(),
        "matched_tick_minute_row_count":30,
        "matched_tick_price_within_candle_range_count":29,
        "per_sample":samples,
    }
    if audit:
        save(a,"joint_comparison",report)
    return report


def test_all_windows_independently_verified_and_read_only(monkeypatch,tmp_path):
    root=tmp_path/"pilot"
    originals={}
    for w in review.WINDOWS:
        originals[w]=make_window(root,w)
    called=[]
    def verifier(*,source_dir,out_dir):
        called.append(Path(source_dir).name)
        assert ".pilot-reverify-" in str(out_dir)
        w=Path(source_dir).name.split("_")[1]
        result=originals[w]
        save(out_dir,"joint_comparison",result)
        return result
    monkeypatch.setattr(review,"compare",verifier)
    out=tmp_path/"final"
    result=review.summarize(root=root,output_dir=out,now=AFTER)
    assert result["status"]=="ALL_THREE_WINDOWS_VERIFIED_RESEARCH_ONLY"
    assert result["verified_triplets"]==12
    assert result["verified_windows"]==3
    assert result["market_ticks_matching_HHMM_minute_labels"]==90
    assert result["market_ticks_within_matched_minute_range"]==87
    assert len(called)==3
    assert result["broker_orders"]==0
    assert result["historical_strategy_returns"] is None
    assert result["backtesting_approval"] is False
    raw=(out/"final_review.json").read_bytes()
    assert sha256(raw).hexdigest()==(out/"final_review.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already written"):
        review.summarize(root=root,output_dir=out,now=AFTER)


def test_missing_window_does_not_become_success(tmp_path,monkeypatch):
    root=tmp_path/"pilot"
    original=make_window(root,"morning")
    monkeypatch.setattr(review,"compare",lambda **_:original)
    result=review.summarize(root=root,now=AFTER,write=False)
    assert result["verified_windows"]==1
    assert result["missing_or_invalid_windows"]==2
    assert result["status"]=="PARTIAL_OR_FAILED_RESEARCH_PILOT"
    assert [s["status"] for s in result["window_results"]]==[
        "VERIFIED_RESEARCH_ONLY","NOT_CAPTURED","NOT_CAPTURED"]


def test_self_consistent_wrong_day_and_wrong_population_still_fail(tmp_path,monkeypatch):
    root=tmp_path/"pilot"
    make_window(root,"morning",date="2026-10-28")
    monkeypatch.setattr(review,"compare",lambda **_:json.loads(
        (root/"2026-10-29_morning_audit"/"joint_comparison.json").read_text()))
    bad=review.summarize(root=root,now=AFTER,write=False)
    assert bad["window_results"][0]["status"]=="EVIDENCE_FAILED_VERIFICATION"
    root2=tmp_path/"other"
    make_window(root2,"morning",bad_symbol=True)
    monkeypatch.setattr(review,"compare",lambda **_:json.loads(
        (root2/"2026-10-29_morning_audit"/"joint_comparison.json").read_text()))
    out=review.summarize(root=root2,now=AFTER,write=False)
    assert out["window_results"][0]["status"]=="EVIDENCE_FAILED_VERIFICATION"


def test_audit_recovery_from_saved_source_without_market_network(tmp_path,monkeypatch):
    root=tmp_path/"pilot"
    original=make_window(root,"morning",audit=False)
    calls=[]
    def offline(*,source_dir,out_dir):
        calls.append(Path(out_dir))
        if Path(out_dir).name.endswith("_audit"):
            save(out_dir,"joint_comparison",original)
        return original
    monkeypatch.setattr(review,"compare",offline)
    result=review.summarize(root=root,now=AFTER,write=True)
    assert result["verified_windows"]==1
    assert result["window_results"][0]["status"]=="VERIFIED_RESEARCH_ONLY"
    assert len(calls)==2  # finish saved-source audit, then independently recheck
    assert (root/"2026-10-29_morning_audit"/"joint_comparison.json").exists()
    assert result["status"]=="PARTIAL_OR_FAILED_RESEARCH_PILOT"


def test_no_final_file_before_session_end(tmp_path):
    root=tmp_path/"pilot"
    root.mkdir()
    with pytest.raises(VenueProbeError,match="market closeout"):
        review.summarize(root=root,now=datetime.fromisoformat(
            "2026-10-29T09:43:00+09:00"),write=True)
    assert not (root/"2026-10-29_final_review").exists()


def test_corrupt_sha_is_not_reported_as_success(tmp_path):
    root=tmp_path/"pilot"
    make_window(root,"morning")
    p=root/"2026-10-29_morning_audit"/"joint_comparison.json"
    p.write_bytes(p.read_bytes()+b"tamper")
    result=review.summarize(root=root,now=AFTER,write=False)
    assert result["window_results"][0]["status"]=="EVIDENCE_FAILED_VERIFICATION"

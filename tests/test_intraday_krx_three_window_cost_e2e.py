"""End-to-end one-day KRX 3-window offline integration, including future cost report.

No KIS network, token issuance, cron, production state or brokerage order APIs.
The production collect/compare/execute/summarize/analyze code is used unchanged;
only KIS GET replies and clock are deterministic test fixtures.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_krx_joint_evidence import collect, ENDPOINTS, SYMBOLS
from quantlab.intraday_krx_joint_pilot import execute
from quantlab.intraday_krx_joint_pilot_review import summarize
from quantlab.intraday_krx_dynamic_cost import analyze
from tests.test_intraday_krx_market_freshness_e2e import _sample
from tests.test_intraday_krx_dynamic_cost import make_l2, make_pit

AFTER=datetime.fromisoformat("2026-10-29T15:46:00+09:00")
WINDOW_TIME={"morning":"09:43:50","midday":"12:35:50",
             "afternoon":"14:35:50"}


def _market_bytes(kind:str,sy:str,window:str)->bytes:
    obj=json.loads(_sample(kind,sy))
    hhmm=WINDOW_TIME[window].replace(":","")[:4]
    if kind=="book":
        obj["output1"]["aspr_acpt_hour"]=hhmm+"50"
    elif kind=="ticks":
        # Mirror the actual KIS inquire-ccnl contract: trade date is absent.
        for idx,row in enumerate(obj["output"]):
            row.pop("stck_bsop_date",None)
            row["stck_cntg_hour"]=hhmm+("50" if idx==0 else "49")
    else:
        obj["output2"][0]["stck_cntg_hour"]=hhmm+"00"
    return json.dumps(obj).encode()


def _full_window(root:Path,window:str,monkeypatch, *,
                 error_kind:str|None=None):
    time=WINDOW_TIME[window]
    now=datetime.fromisoformat(f"2026-10-29T{time}+09:00")
    calls=[]
    t=0
    def clock():
        nonlocal t
        result=now+timedelta(milliseconds=t*100)
        t+=1
        return result.astimezone(timezone.utc)
    class Response:
        def __init__(self,payload):self.content=payload
        def raise_for_status(self):pass
    def getter(url,params,headers,timeout):
        kind=next(k for k,(u,tr) in ENDPOINTS.items()
                  if url==u and tr==headers["tr_id"])
        sy=params["FID_INPUT_ISCD"]
        assert "CANO" not in params
        assert params["FID_COND_MRKT_DIV_CODE"]=="J"
        calls.append((sy,kind))
        if error_kind==kind:
            raise RuntimeError("synthetic provider fail; must redact credentials")
        return Response(_market_bytes(kind,sy,window))
    def collector(**kwargs):
        return collect(**kwargs,clock=clock,getter=getter,
                       sleeper=lambda _:None)
    kwargs={"session":"2026-10-29","window":window,"root":root,
            "clock":lambda:now,"token_supplier":lambda:"test-bearer",
            "collector":collector}
    return kwargs,calls


def test_full_three_window_capture_to_independently_audited_cost(tmp_path,monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY","synthetic-key")
    monkeypatch.setenv("KIS_APP_SECRET","synthetic-secret")
    root=tmp_path/"pilot"
    collected=0
    for window in WINDOW_TIME:
        kwargs,calls=_full_window(root,window,monkeypatch)
        report=execute(**kwargs)
        assert report["status"]=="AUDITED_RESEARCH_ONLY"
        assert report["sample_triplets"]==4
        assert len(calls)==12
        collected+=len(calls)
        assert execute(**{**kwargs,"collector":lambda **_:pytest.fail(
            "duplicate market read"),"token_supplier":lambda:pytest.fail(
            "duplicate OAuth")})["status"]=="ALREADY_AUDITED"
    assert collected==36
    review=summarize(root=root,now=AFTER,write=True)
    assert review["verified_windows"]==3
    assert review["verified_triplets"]==12
    assert review["status"]=="ALL_THREE_WINDOWS_VERIFIED_RESEARCH_ONLY"
    assert review["market_ticks_matching_HHMM_minute_labels"]==24
    assert review["market_ticks_within_matched_minute_range"]==24
    assert review["cross_source_price_consistency_observed_only"]==(
        "ALL_OBSERVED_MATCHES_WITHIN_CANDLE_RANGE_NOT_CERTIFIED")
    assert review["trade_day_independently_certified"] is False
    assert all(x["tick_business_date_missing_triplets"]==4
               for x in review["window_results"])
    l2=make_l2(tmp_path)
    pit=make_pit(tmp_path)
    cost=analyze(output_dir=tmp_path/"cost",l2_roots=[l2],
                 pilot_root=root,pit_dir=pit)
    assert cost["snapshot_count"]==14
    assert set(cost["by_sample_group"])=={
        "2026-10-28_L2","2026-10-29_morning",
        "2026-10-29_midday","2026-10-29_afternoon"}
    assert [cost["by_sample_group"][f"2026-10-29_{w}"][s]["quote_count"]
            for w in WINDOW_TIME for s in SYMBOLS]==[2]*6
    assert cost["actual_broker_fill_records"]==0
    assert cost["strategy_returns"] is None
    assert cost["eligible_for_live_or_paper_trading"] is False
    for folder,name in ((root/"2026-10-29_final_review","final_review"),
                        (tmp_path/"cost","cost_sensitivity")):
        raw=(folder/f"{name}.json").read_bytes()
        assert sha256(raw).hexdigest()==(
            folder/f"{name}.sha256").read_text().strip()
    assert not list(root.glob("*failure*"))


def test_partial_market_failure_preserved_and_remaining_window_costed(
        tmp_path,monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY","synthetic-key")
    monkeypatch.setenv("KIS_APP_SECRET","synthetic-secret")
    root=tmp_path/"pilot"
    kwargs,calls=_full_window(root,"morning",monkeypatch)
    assert execute(**kwargs)["status"]=="AUDITED_RESEARCH_ONLY"
    kwargs,calls=_full_window(root,"midday",monkeypatch,error_kind="ticks")
    with pytest.raises(RuntimeError,match="synthetic provider"):
        execute(**kwargs)
    assert (root/"2026-10-29_midday_failure.json").is_file()
    kwargs,calls=_full_window(root,"afternoon",monkeypatch)
    assert execute(**kwargs)["status"]=="AUDITED_RESEARCH_ONLY"
    review=summarize(root=root,now=AFTER,write=True)
    assert review["verified_windows"]==2
    assert review["window_results"][1]["status"]=="MARKET_CAPTURE_FAILED_RECORDED"
    assert review["status"]=="PARTIAL_OR_FAILED_RESEARCH_PILOT"
    l2=make_l2(tmp_path)
    pit=make_pit(tmp_path)
    cost=analyze(output_dir=tmp_path/"cost",l2_roots=[l2],
                 pilot_root=root,pit_dir=pit)
    assert cost["snapshot_count"]==10  # 2 historic + 2 windows * 4 book samples
    assert "2026-10-29_midday" not in cost["by_sample_group"]
    assert next(x for x in cost["sources"] if x["kind"]=="joint_pilot")[
        "missing_or_invalid_windows"]==1
    assert cost["eligible_for_live_or_paper_trading"] is False

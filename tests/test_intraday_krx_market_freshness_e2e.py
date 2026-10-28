"""KRX one-day (2026-10-29) exact pipeline integration: 12 mocked KIS GETs.

Tests raw bytes, normalized SHA, real audit, offline closeout and no repeat
token/order calls without accessing real brokerage or changing running cron.
"""
from __future__ import annotations

from datetime import datetime,timezone,timedelta
from hashlib import sha256
import json
from pathlib import Path
import pytest

from quantlab.intraday_kis_venues import VenueProbeError
from quantlab.intraday_krx_joint_evidence import (
    _decode_market,collect,ENDPOINTS,SYMBOLS,
)
from quantlab.intraday_krx_joint_pilot import execute
from quantlab.intraday_krx_joint_pilot_review import summarize

NOW=datetime.fromisoformat("2026-10-29T09:43:50+09:00")
AFTER=datetime.fromisoformat("2026-10-29T15:46:00+09:00")


def _sample(kind:str,sy:str):
    ask=242000 if sy=="005930" else 1_483_000
    bid=ask-(500 if sy=="005930" else 1000)
    step=ask-bid
    if kind=="book":
        one={"aspr_acpt_hour":"094350"}
        for i in range(1,11):
            one[f"askp{i}"]=str(ask+(i-1)*step)
            one[f"bidp{i}"]=str(bid-(i-1)*step)
            one[f"askp_rsqn{i}"]="1000"
            one[f"bidp_rsqn{i}"]="1000"
        data={"rt_cd":"0","output1":one,
              "output2":{"stck_shrn_iscd":sy,"stck_prpr":str(bid)}}
    elif kind=="ticks":
        data={"rt_cd":"0","output":[
            {"stck_bsop_date":"20261029","stck_cntg_hour":"094350",
             "stck_prpr":str(bid),"cntg_vol":"20"},
            {"stck_bsop_date":"20261029","stck_cntg_hour":"094349",
             "stck_prpr":str(bid),"cntg_vol":"10"},
        ]}
    else:
        data={"rt_cd":"0","output1":{"stck_prpr":str(bid)},
              "output2":[{
                  "stck_bsop_date":"20261029","stck_cntg_hour":"094300",
                  "stck_oprc":str(bid),"stck_hgpr":str(ask),
                  "stck_lwpr":str(bid),"stck_prpr":str(bid),
                  "cntg_vol":"3000",
              }]}
    return json.dumps(data).encode()


def test_full_live_style_pilot_offline_only_then_final_SHA_closeout(tmp_path,monkeypatch):
    # The pilot calls the real collect() and compare(), only the network GET
    # and clocks are deterministic. Protect against accidental broker orders.
    monkeypatch.setenv("KIS_APP_KEY","synthetic-not-real")
    monkeypatch.setenv("KIS_APP_SECRET","synthetic-not-real")
    class Response:
        def __init__(self,data):self.content=data
        def raise_for_status(self):pass
    requests=[]
    def getter(url,params,headers,timeout):
        assert "CANO" not in params and "ACNT_PRDT_CD" not in params
        assert params["FID_COND_MRKT_DIV_CODE"]=="J"
        sy=params["FID_INPUT_ISCD"]
        kind=next(k for k,(endpoint,tr) in ENDPOINTS.items()
                  if url==endpoint and headers["tr_id"]==tr)
        requests.append((sy,kind))
        return Response(_sample(kind,sy))
    ticks=0
    def clock():
        nonlocal ticks
        result=NOW+timedelta(milliseconds=100*ticks)
        ticks+=1
        return result.astimezone(timezone.utc)
    def collector(**kw):
        return collect(**kw,clock=clock,getter=getter,sleeper=lambda _:None)
    root=tmp_path/"pilot"
    args=dict(session="2026-10-29",window="morning",root=root,
              clock=lambda:NOW,token_supplier=lambda:"fake-readable-token",
              collector=collector)
    status=execute(**args)
    assert status["status"]=="AUDITED_RESEARCH_ONLY"
    assert status["sample_triplets"]==4
    assert requests==[(sy,kind) for _ in range(2)
                      for sy in SYMBOLS for kind in ENDPOINTS]
    assert len(requests)==12
    assert execute(**{**args,"token_supplier":lambda:pytest.fail(
        "must not contact OAuth again"),"collector":lambda **_:pytest.fail(
        "must not re-collect")})["status"]=="ALREADY_AUDITED"
    review=summarize(root=root,now=AFTER,write=True)
    assert review["verified_windows"]==1
    assert review["status"]=="PARTIAL_OR_FAILED_RESEARCH_PILOT"
    assert review["window_results"][0]["verified_triplets"]==4
    morning_audit=json.loads(
        (root/"2026-10-29_morning_audit"/"joint_comparison.json").read_text())
    assert morning_audit["sample_triplets_with_all_trade_date_fields"]==4
    assert morning_audit["sample_triplets_without_trade_date_fields"]==0
    assert morning_audit["trade_date_independently_certified"] is False
    assert all(x["tick_business_date_field_on_all_rows"] is True
               and x["trade_date_independently_certified"] is False
               for x in morning_audit["per_sample"])
    assert review["window_results"][1]["status"]=="NOT_CAPTURED"
    assert review["window_results"][2]["status"]=="NOT_CAPTURED"
    assert review["historical_strategy_returns"] is None
    audit=root/"2026-10-29_final_review"
    assert sha256((audit/"final_review.json").read_bytes()).hexdigest()==(
        audit/"final_review.sha256").read_text().strip()
    assert not list(root.glob("*failure*"))
    assert all(b"synthetic-not-real" not in p.read_bytes()
               and b"fake-readable-token" not in p.read_bytes()
               for p in root.rglob("*.json"))


def test_outdated_market_ticks_even_if_hash_verified_fail_closed():
    received=datetime.fromisoformat("2026-10-29T09:43:50+09:00")
    raw=json.loads(_sample("ticks","005930"))
    for entry in raw["output"]:
        entry["stck_cntg_hour"]="093800"
    with pytest.raises(VenueProbeError,match="Stale or future"):
        _decode_market(json.dumps(raw).encode(),"ticks","005930",received)


def test_tick_other_day_with_same_clock_cannot_be_accepted():
    received=datetime.fromisoformat("2026-10-29T09:43:50+09:00")
    raw=json.loads(_sample("ticks","005930"))
    raw["output"][0]["stck_bsop_date"]="20261028"
    with pytest.raises(VenueProbeError,match="Other-date"):
        _decode_market(json.dumps(raw).encode(),"ticks","005930",received)


def test_stale_minute_candle_and_invalid_clock_cannot_be_accepted():
    received=datetime.fromisoformat("2026-10-29T09:43:50+09:00")
    raw=json.loads(_sample("minute","005930"))
    raw["output2"][0]["stck_cntg_hour"]="092000"
    with pytest.raises(VenueProbeError,match="Stale or future"):
        _decode_market(json.dumps(raw).encode(),"minute","005930",received)
    raw["output2"][0]["stck_cntg_hour"]="296100"
    with pytest.raises(VenueProbeError,match="Invalid market time"):
        _decode_market(json.dumps(raw).encode(),"minute","005930",received)

def test_real_kis_inquire_ccnl_like_ticks_have_no_business_date_field():
    received=datetime.fromisoformat("2026-10-29T09:43:50+09:00")
    body=json.loads(_sample("ticks","005930"))
    for row in body["output"]:
        row.pop("stck_bsop_date",None)  # actual observed KIS off-hours schema
    result=_decode_market(json.dumps(body).encode(),"ticks","005930",received)
    assert result["provider_business_date_field_on_all_rows"] is False
    assert result["market_date_independently_certified"] is False
    assert result["latest_row_label_age_seconds"]==0
    assert len(result["rows"])==2
    assert "stck_bsop_date" not in result["rows"][0]


def test_mixed_trade_date_claims_do_not_certify_whole_response():
    received=datetime.fromisoformat("2026-10-29T09:43:50+09:00")
    body=json.loads(_sample("ticks","005930"))
    body["output"][0].pop("stck_bsop_date")
    result=_decode_market(json.dumps(body).encode(),"ticks","005930",received)
    assert result["provider_business_date_field_on_all_rows"] is False
    assert result["market_date_independently_certified"] is False

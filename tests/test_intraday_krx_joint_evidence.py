"""KIS read-only book, recent exchange ticks and unfinished 1m tests."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json

import pytest

from quantlab.intraday_krx_joint_evidence import (
    _decode_market,collect,compare,ENDPOINTS,SYMBOLS,
)
from quantlab.intraday_kis_venues import VenueProbeError

BASE=datetime.fromisoformat("2026-10-28T15:17:50+09:00")


def sample(kind, sy):
    if kind=="book":
        ask=242000 if sy=="005930" else 1483000
        bid=ask-(500 if sy=="005930" else 1000)
        step=ask-bid
        data={"aspr_acpt_hour":"151749"}
        for i in range(1,11):
            data[f"askp{i}"]=str(ask+(i-1)*step)
            data[f"bidp{i}"]=str(bid-(i-1)*step)
            data[f"askp_rsqn{i}"]="100"
            data[f"bidp_rsqn{i}"]="100"
        return json.dumps({"rt_cd":"0","output1":data,
                           "output2":{"stck_shrn_iscd":sy,
                                      "stck_prpr":str(bid)}}).encode()
    price=241750 if sy=="005930" else 1490000
    if kind=="ticks":
        return json.dumps({"rt_cd":"0","output":[
            {"stck_cntg_hour":"151750","stck_prpr":str(price),"cntg_vol":"20"},
            {"stck_cntg_hour":"151749","stck_prpr":str(price),"cntg_vol":"1"},
        ]}).encode()
    return json.dumps({"rt_cd":"0","output1":{"stck_prpr":str(price)},
                       "output2":[{
                           "stck_bsop_date":"20261028",
                           "stck_cntg_hour":"151700",
                           "stck_oprc":str(price),
                           "stck_hgpr":str(price+1000),
                           "stck_lwpr":str(price-1000),
                           "stck_prpr":str(price),"cntg_vol":"500",
                       }]}).encode()


def fixture(tmp_path):
    class Response:
        def __init__(self,body): self.content=body
        def raise_for_status(self):pass
    sequence=[]
    clock_step=0
    def tick_clock():
        nonlocal clock_step
        value=BASE+timedelta(milliseconds=clock_step*100)
        clock_step+=1
        return value.astimezone(timezone.utc)
    def get(url,params,headers,timeout):
        assert headers["tr_id"] in {pair[1] for pair in ENDPOINTS.values()}
        assert set(params).issubset({"FID_COND_MRKT_DIV_CODE","FID_INPUT_ISCD",
                                    "FID_INPUT_HOUR_1","FID_PW_DATA_INCU_YN",
                                    "FID_ETC_CLS_CODE"})
        assert params["FID_COND_MRKT_DIV_CODE"]=="J"
        assert "CANO" not in params and "ACNT_PRDT_CD" not in params
        sy=params["FID_INPUT_ISCD"]
        kind=next(k for k,(endpoint,_) in ENDPOINTS.items() if url==endpoint)
        sequence.append((sy,kind))
        return Response(sample(kind,sy))
    output=tmp_path/"raw"
    report=collect(out_dir=output,key="KEY",secret="SECRET",token="TOKEN",
                   count=1,clock=tick_clock,sleeper=lambda _:None,
                   getter=get)
    assert sequence==[(sy,kind) for sy in SYMBOLS for kind in ENDPOINTS]
    assert report["real_orders"]==0
    assert not any(b"SECRET" in p.read_bytes() or b"TOKEN" in p.read_bytes()
                   for p in output.glob("*.json"))
    return output


def test_full_offline_joint_comparison_with_receipt_provenance(tmp_path):
    evidence=fixture(tmp_path)
    report=compare(source_dir=evidence,out_dir=tmp_path/"output")
    assert report["sample_triplets"]==2
    assert report["matched_tick_minute_row_count"]==4
    assert report["matched_tick_price_within_candle_range_count"]==4
    assert report["broker_orders"]==0 and report["strategy_returns"] is None
    assert report["KIS_minute_start_end_semantics_confirmed"] is False
    assert report["sample_triplets_without_trade_date_fields"]==2
    assert report["sample_triplets_with_all_trade_date_fields"]==0
    assert report["trade_date_independently_certified"] is False
    assert all(not s["same_instant_L2_tick_minute"] for s in report["per_sample"])
    saved=tmp_path/"output"/"joint_comparison.json"
    assert sha256(saved.read_bytes()).hexdigest()==(
        tmp_path/"output"/"joint_comparison.sha256").read_text().strip()


def test_tampered_raw_market_data_is_rejected(tmp_path):
    evidence=fixture(tmp_path)
    path=evidence/"raw-000-005930-ticks.json"
    path.write_bytes(path.read_bytes()+b"TAMPER")
    with pytest.raises(VenueProbeError,match="checksum"):
        compare(source_dir=evidence,out_dir=tmp_path/"output")
    assert not (tmp_path/"output").exists()


def test_no_closed_market_or_actual_broker_fills(tmp_path):
    with pytest.raises(VenueProbeError,match="outside"):
        collect(out_dir=tmp_path/"closed",key="k",secret="s",token="t",
                count=1,clock=lambda:datetime.fromisoformat(
                    "2026-10-28T15:24:00+09:00"))
    assert not (tmp_path/"closed").exists()
    with pytest.raises(VenueProbeError,match="Invalid market time"):
        _decode_market(json.dumps({"rt_cd":"0","output":[
            {"stck_cntg_hour":"BAD","stck_prpr":"101","cntg_vol":"10"}]}).encode(),
            "ticks","005930",BASE)


def test_provider_missing_or_wrong_date_is_rejected():
    raw=json.dumps({"rt_cd":"0","output1":{},"output2":[
        {"stck_bsop_date":"20261027","stck_cntg_hour":"151700",
         "stck_oprc":"100","stck_hgpr":"110","stck_lwpr":"90",
         "stck_prpr":"100","cntg_vol":"20"},
    ]}).encode()
    with pytest.raises(VenueProbeError,match="Other-date"):
        _decode_market(raw,"minute","005930",BASE)

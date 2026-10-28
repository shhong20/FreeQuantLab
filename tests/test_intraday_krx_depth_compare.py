"""KRX live quote comparison: fixture depth, chronology, source SHA, no fills."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_krx_depth_compare import (
    collect,compare,parse_quote,sweep_visible,SYMBOLS,
)
from quantlab.intraday_kis_venues import VenueProbeError


RECEIVED=datetime.fromisoformat("2026-10-28T13:20:40+09:00")


def quote_raw(symbol="005930", *,label="132039", crossed=False):
    first=242000 if symbol=="005930" else 1483000
    step=500 if symbol=="005930" else 1000
    bid=first-500 if symbol=="005930" else first-1000
    a={f"askp{i}":str(first+(i-1)*step) for i in range(1,11)}
    a.update({f"bidp{i}":str(bid-(i-1)*step) for i in range(1,11)})
    a.update({f"askp_rsqn{i}":str(100 if i==1 else 50) for i in range(1,11)})
    a.update({f"bidp_rsqn{i}":str(20 if i==1 else 50) for i in range(1,11)})
    if crossed:
        a["bidp1"]=a["askp1"]
    a["aspr_acpt_hour"]=label
    return json.dumps({"rt_cd":"0","output1":a,
                       "output2":{"stck_shrn_iscd":symbol,
                                  "stck_prpr":str(first)}}).encode()


def pit_fixture(tmp_path):
    from datetime import date,timedelta
    d=date(2026,6,1)
    sessions=[]
    while len(sessions)<60:
        if d.weekday()<5:
            sessions.append(d.isoformat())
        d+=timedelta(days=1)
    folder=tmp_path/"pit"
    folder.mkdir()
    src={
        "status":"PIT_RESEARCH_PROVISIONAL_NO_CERTIFIED_FILLS_OR_PNL",
        "all_session_dates_preserved":sessions,
        "population_stock_days":len(sessions)*len(SYMBOLS),
        "broker_fills_verified":False,
        "certified_pnl_or_sharpe_calculated":False,
    }
    payload=(json.dumps(src,sort_keys=True)+"\n").encode()
    (folder/"pit_replay.json").write_bytes(payload)
    (folder/"pit_replay.sha256").write_text(sha256(payload).hexdigest()+"\n")
    return folder


def test_quoted_depth_vs_constant_five_bps_is_not_fill_proof():
    q=parse_quote(quote_raw(), "005930", RECEIVED)
    assert q["quote_age_seconds"]==1
    assert q["best_ask"]==242000
    assert q["best_bid"]==241500
    buy=sweep_visible(q,"BUY",10)
    assert buy["visible_depth_sufficient"] is True
    assert buy["visible_mid_cost_exceeds_5bps"] is True
    assert buy["possible_fill_not_certified"] is True
    assert buy["visible_adverse_vs_mid_bps"]==pytest.approx(
        (242000/241750-1)*10000)
    sell=sweep_visible(q,"SELL",100)
    assert sell["visible_depth_sufficient"] is True
    assert sell["visible_extra_vs_best_bps"]>0
    assert sell["visible_adverse_vs_mid_bps"]>buy["visible_adverse_vs_mid_bps"]


def test_refuse_stale_crossed_wrong_ticker_and_outside_hours():
    with pytest.raises(VenueProbeError,match="Stale"):
        parse_quote(quote_raw(label="130000"),"005930",RECEIVED)
    with pytest.raises(VenueProbeError,match="crossed"):
        parse_quote(quote_raw(crossed=True),"005930",RECEIVED)
    with pytest.raises(VenueProbeError,match="identity"):
        parse_quote(quote_raw("005930"),"000660",RECEIVED)
    with pytest.raises(VenueProbeError,match="outside"):
        parse_quote(quote_raw(label="152030"),"005930",RECEIVED)


def test_visible_10_level_insufficiency_is_not_filled():
    q=parse_quote(quote_raw(),"005930",RECEIVED)
    order=sweep_visible(q,"BUY",10_000)
    assert order["visible_depth_sufficient"] is False
    assert order["hypothetical_sweep_vwap"] is None
    assert order["visible_mid_cost_exceeds_5bps"] is None


def test_fixtures_capture_compare_and_raw_tampering_fail_closed(tmp_path):
    class R:
        def __init__(self,body):self.content=body
        def raise_for_status(self):pass
    calls=[]
    def get(url,params,headers,timeout):
        assert url.endswith("/inquire-asking-price-exp-ccn")
        assert headers["tr_id"]=="FHKST01010200"
        assert set(params)=={"FID_COND_MRKT_DIV_CODE","FID_INPUT_ISCD"}
        assert params["FID_COND_MRKT_DIV_CODE"]=="J"
        calls.append(params["FID_INPUT_ISCD"])
        return R(quote_raw(params["FID_INPUT_ISCD"]))
    src=tmp_path/"quotes"
    result=collect(output_dir=src,key="key",secret="secret",token="token",
                   count=1,getter=get,
                   clock=lambda:RECEIVED.astimezone(timezone.utc))
    assert calls==list(SYMBOLS)
    assert result["real_broker_fills"]==0
    for sy in SYMBOLS:
        raw=(src/f"raw-000-{sy}.json").read_bytes()
        assert b"secret" not in raw
        receipt=json.loads((src/f"quote-000-{sy}.json").read_bytes())
        assert receipt["source_raw_sha256"]==sha256(raw).hexdigest()
    pit=pit_fixture(tmp_path)
    report=compare(snapshot_dir=src,pit_dir=pit,output_dir=tmp_path/"report")
    assert report["historical_quote_date_alignment"] is False
    assert report["retrospective_strategy_returns_from_this_depth"] is None
    assert report["realized_strategy_returns"] is None
    assert report["order_submissions"]==0
    assert report["by_symbol"]["005930"]["by_share_size"]["10"]["BUY"][
        "observed_mid_adverse_above_assumed_5bps_count"]==1
    saved=tmp_path/"report"/"depth_comparison.json"
    assert sha256(saved.read_bytes()).hexdigest()==(
        tmp_path/"report"/"depth_comparison.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already exists"):
        compare(snapshot_dir=src,pit_dir=pit,output_dir=tmp_path/"report")
    p=src/"raw-000-005930.json"
    p.write_bytes(p.read_bytes()+b"tamper")
    with pytest.raises(VenueProbeError,match="checksum"):
        compare(snapshot_dir=src,pit_dir=pit,output_dir=tmp_path/"tampered")
    assert not (tmp_path/"tampered").exists()


def test_reject_snapshot_on_historical_day_without_exact_order_alignment(tmp_path):
    pit=pit_fixture(tmp_path)
    payload=json.loads((pit/"pit_replay.json").read_text())
    payload["all_session_dates_preserved"][-1]="2026-10-28"
    body=(json.dumps(payload,sort_keys=True)+"\n").encode()
    (pit/"pit_replay.json").write_bytes(body)
    (pit/"pit_replay.sha256").write_text(sha256(body).hexdigest()+"\n")
    # The quote capture should never be interpreted as a historical order.
    class Response:
        def __init__(self,b):self.content=b
        def raise_for_status(self):pass
    quotes=tmp_path/"quotes"
    collect(output_dir=quotes,key="x",secret="y",token="z",count=1,
            getter=lambda _,**params:Response(
                quote_raw(params["params"]["FID_INPUT_ISCD"])),
            clock=lambda:RECEIVED.astimezone(timezone.utc))
    with pytest.raises(VenueProbeError,match="exact-time"):
        compare(snapshot_dir=quotes,pit_dir=pit,output_dir=tmp_path/"result")

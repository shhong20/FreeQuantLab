"""Official public quote integrity, visible L2 sweep and anti-overclaim tests."""
from __future__ import annotations

import json
from datetime import datetime,timezone,timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from quantlab.continuous_portfolio import ASSETS
from quantlab.live_orderbook_evidence import (
    _check_book,fetch_public_snapshot,collect_snapshots,analyze_snapshots,
    sweep,prior_notional_medians,ORDER_NOTIONALS,MAX_AGE_SECONDS,
)


def fake_books(ts=None,depth=30):
    ts=ts or int(datetime.now(timezone.utc).timestamp()*1000)
    out=[]
    for market in ASSETS:
        asks=[{"ask_price":101.+i,"bid_price":99.-i,
               "ask_size":1.,"bid_size":1.} for i in range(depth)]
        out.append({"market":market,"timestamp":ts,"level":0,
                    "orderbook_units":asks,
                    "total_bid_size":depth,"total_ask_size":depth})
    return out


def test_crossed_unsorted_duplicate_stale_missing_negative_levels_fail():
    received=datetime.now(timezone.utc)
    good=fake_books(int(received.timestamp()*1000))[0]
    assert _check_book(good,received)["levels"]==30
    def reject(transform,word):
        bad=json.loads(json.dumps(good))
        transform(bad)
        with pytest.raises(ValueError,match=word):
            _check_book(bad,received)
    reject(lambda b:b.update(timestamp=b["timestamp"]-int((MAX_AGE_SECONDS+2)*1000)),
           "Stale")
    reject(lambda b:b["orderbook_units"][0].update(ask_price=98),"Crossed")
    reject(lambda b:b["orderbook_units"][1].update(ask_price=101),"Unsorted")
    reject(lambda b:b["orderbook_units"][0].update(ask_size=-1),"Zero")
    reject(lambda b:b.update(level=10000),"Aggregated")
    reject(lambda b:b.update(market="KRW-UNKNOWN"),"unknown")
    reject(lambda b:b.update(orderbook_units=[]),"depth")


def test_visible_sweep_midpoint_and_partial_coverage_are_not_execution():
    book=_check_book(fake_books()[0],datetime.now(timezone.utc))
    buy=sweep(book,"BUY",101)
    assert buy["complete_visible_depth"]
    assert buy["full_fill_vwap_krw"]==pytest.approx(101)
    assert buy["depth_vwap_vs_best_bps"]==pytest.approx(0)
    assert buy["best_vs_mid_bps"]==pytest.approx(100)
    long_buy=sweep(book,"BUY",202)
    assert long_buy["complete_visible_depth"]
    assert long_buy["full_fill_vwap_krw"]==pytest.approx(101.5)
    assert long_buy["depth_vwap_vs_best_bps"]>0
    sell=sweep(book,"SELL",198)
    assert sell["complete_visible_depth"]
    assert sell["full_fill_vwap_krw"]==pytest.approx(98.5)
    exceeds=sweep(book,"BUY",10_000)
    assert not exceeds["complete_visible_depth"]
    assert exceeds["full_fill_vwap_krw"] is None
    assert exceeds["visible_filled_fraction"]<1


def test_fresh_rest_snapshot_requires_all_four_pairs_and_correct_batch():
    now=datetime.now(timezone.utc)
    class Resp:
        content=json.dumps(fake_books(int(now.timestamp()*1000))).encode()
        def raise_for_status(self): pass
    class Session:
        def get(self,url,**kwargs):
            assert url=="https://api.upbit.com/v1/orderbook"
            assert kwargs["params"]["count"]==30
            assert kwargs["params"]["markets"]==",".join(ASSETS)
            assert "Authorization" not in kwargs["headers"]
            return Resp()
    result=fetch_public_snapshot(session=Session(),now=now)
    assert len(result["books"])==4
    assert result["raw_payload_sha256"]==sha256(result["raw_payload_utf8"].encode()).hexdigest()


def test_sealed_manifest_and_raw_sha_analysis_fail_closed(tmp_path):
    time=datetime.now(timezone.utc)
    index=[0]
    def getter():
        index[0]+=1
        ts=int((time+timedelta(seconds=3*index[0])).timestamp()*1000)
        received=time+timedelta(seconds=3*index[0])
        books=fake_books(ts)
        raw=json.dumps(books).encode()
        return {"requested_utc":received.isoformat(),
                "received_utc":received.isoformat(),
                "request_rtt_seconds":.001,
                "url":"https://api.upbit.com/v1/orderbook",
                "query":{"markets":list(ASSETS),"count":30},
                "raw_payload_sha256":sha256(raw).hexdigest(),
                "raw_payload_utf8":raw.decode(),
                "books":books}
    root=tmp_path/"snap"
    collected=collect_snapshots(output_dir=root,count=2,spacing_seconds=1,
                                getter=getter,sleeper=lambda x:None)
    assert collected["captured"]==2
    report=analyze_snapshots(snapshot_dir=root,output_dir=tmp_path/"result",
                              volume_medians={s:1e10 for s in ASSETS})
    assert report["status"]=="EXPLORATORY_ONLY_NOT_CALIBRATION"
    assert report["four_market_rows"]==2*4*2*len(ORDER_NOTIONALS)
    assert not report["no_model_fitting_or_strategy_PnL_changes"] is False
    assert report["raw_capture_count"]==2
    assert any(x["incomplete_book_snapshots"]>0 for x in report["summaries"])
    with pytest.raises(ValueError,match="overwrite"):
        collect_snapshots(output_dir=root,count=2)
    with pytest.raises(ValueError,match="overwrite"):
        analyze_snapshots(snapshot_dir=root,output_dir=tmp_path/"result")
    (root/"raw-000.json").write_text("tampered")
    with pytest.raises(ValueError,match="SHA"):
        analyze_snapshots(snapshot_dir=root,output_dir=tmp_path/"unused")


def test_duplicate_exchange_timestamp_not_an_independent_quote(tmp_path):
    now=datetime.now(timezone.utc)
    books=fake_books(int(now.timestamp()*1000))
    root=tmp_path/"c"
    root.mkdir()
    entries=[]
    for i in range(2):
        raw=json.dumps(books).encode()
        name=f"raw-{i:03d}.json"
        rec=f"book-{i:03d}.json"
        (root/name).write_bytes(raw)
        (root/rec).write_text(json.dumps({
            "received_utc":now.isoformat(),
            "raw_payload_sha256":sha256(raw).hexdigest(),
        }))
        entries.append({"raw":name,"receipt":rec,"sha256":sha256(raw).hexdigest(),
                        "received_utc":now.isoformat()})
    (root/"manifest.json").write_text(json.dumps({
        "status":"PUBLIC_READ_ONLY_ORDERBOOK_SNAPSHOTS","captured":2,"samples":entries
    }))
    with pytest.raises(ValueError,match="Duplicate"):
        analyze_snapshots(snapshot_dir=root,output_dir=tmp_path/"out")


def test_snapshot_reference_volume_exact_day_not_future_day():
    snap=Path("/home/hong/apps/FreeQuantLab-crossmarket-20261021/reports/cross-market-snapshot-20261021")
    hist=Path("/home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021")
    if not snap.is_dir() or not hist.is_dir():
        pytest.skip("Original hashed source snapshots only exist on WSL")
    data=prior_notional_medians(cross_snapshot=snap,historical_snapshot=hist,
        quote_utc=datetime(2026,10,21,5,0,tzinfo=timezone.utc))
    assert all(data[s]>0 for s in ASSETS)
    with pytest.raises(ValueError,match="not current"):
        prior_notional_medians(cross_snapshot=snap,historical_snapshot=hist,
            quote_utc=datetime(2026,10,22,5,0,tzinfo=timezone.utc))

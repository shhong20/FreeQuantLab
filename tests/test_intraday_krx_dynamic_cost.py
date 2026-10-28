"""SHA-anchored KRX depth cost sensitivity, no broker fill assumptions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_krx_dynamic_cost import analyze, _cost_record, _load_l2
from quantlab.intraday_krx_depth_compare import parse_quote, SYMBOLS
from quantlab.intraday_kis_venues import VenueProbeError

RECEIVED=datetime.fromisoformat("2026-10-28T13:20:40+09:00")


def _bytes(sy:str, *,askq:int=1000,bidq:int=1000):
    first=242000 if sy=="005930" else 1_483_000
    step=500 if sy=="005930" else 1000
    last=first-step
    out={"aspr_acpt_hour":"132039"}
    for i in range(1,11):
        out[f"askp{i}"]=str(first+(i-1)*step)
        out[f"bidp{i}"]=str(last-(i-1)*step)
        out[f"askp_rsqn{i}"]=str(askq)
        out[f"bidp_rsqn{i}"]=str(bidq)
    return json.dumps({"rt_cd":"0","output1":out,
                       "output2":{"stck_shrn_iscd":sy,
                                  "stck_prpr":str(last)}}).encode()


def _save(folder:Path,stem:str,doc:dict):
    folder.mkdir(parents=True,exist_ok=True)
    raw=(json.dumps(doc,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
    (folder/f"{stem}.json").write_bytes(raw)
    (folder/f"{stem}.sha256").write_text(sha256(raw).hexdigest()+"\n")
    return raw


def make_l2(tmp_path:Path, *,askq=1000,bidq=1000):
    folder=tmp_path/"l2"
    folder.mkdir()
    manifests=[]
    for sy in SYMBOLS:
        k=0
        raw=_bytes(sy,askq=askq,bidq=bidq)
        q=parse_quote(raw,sy,RECEIVED)
        quote=(json.dumps(q,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        (folder/f"raw-{k:03d}-{sy}.json").write_bytes(raw)
        (folder/f"quote-{k:03d}-{sy}.json").write_bytes(quote)
        manifests.append({
            "symbol":sy,"index":k,
            "raw_file":f"raw-{k:03d}-{sy}.json",
            "filename":f"quote-{k:03d}-{sy}.json",
            "raw_payload_sha256":sha256(raw).hexdigest(),
            "receipt_sha256":sha256(quote).hexdigest(),
        })
    _save(folder,"manifest",{
        "status":"KIS_KRX_LIVE_L2_READONLY_NO_FILLS",
        "symbols":list(SYMBOLS),
        "endpoint":"https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        "tr_id":"FHKST01010200",
        "samples_per_symbol":1,
        "historical_orderbook_coverage":False,
        "real_broker_fills":0,
        "quotes":manifests,
    })
    return folder


def make_pit(tmp_path:Path, *,last="2026-10-23"):
    folder=tmp_path/"pit"
    folder.mkdir()
    start=datetime(2026,5,1)
    sessions=[]
    while len(sessions)<99:
        d=start+timedelta(days=len(sessions))
        sessions.append(d.strftime("%Y-%m-%d"))
    sessions.append(last)
    sessions.sort()
    _save(folder,"pit_replay",{
        "status":"PIT_RESEARCH_PROVISIONAL_NO_CERTIFIED_FILLS_OR_PNL",
        "all_session_dates_preserved":sessions,
        "broker_fills_verified":False,
        "certified_pnl_or_sharpe_calculated":False,
    })
    return folder


def test_real_quote_provenance_and_budget_lot_guard(tmp_path):
    src=make_l2(tmp_path)
    pit=make_pit(tmp_path)
    out=tmp_path/"result"
    data=analyze(l2_roots=[src],pit_dir=pit,output_dir=out)
    assert data["snapshot_count"]==2
    assert data["actual_broker_fill_records"]==0
    assert data["strategy_returns"] is None
    assert data["eligible_for_live_or_paper_trading"] is False
    samsung=data["by_sample_group"]["2026-10-28_L2"]["005930"]
    hynix=data["by_sample_group"]["2026-10-28_L2"]["000660"]
    assert samsung["halfspread_bps"]["median"]==pytest.approx(500/241750*5000)
    assert hynix["halfspread_bps"]["median"]==pytest.approx(1000/1_482_500*5000)
    assert hynix["by_budget_krw"]["1000000"]["BUY"]["zero_share_budget_count"]==1
    assert hynix["by_budget_krw"]["1000000"]["BUY"]["cost_bps"]["median"] is None
    assert samsung["by_budget_krw"]["1000000"]["BUY"]["zero_share_budget_count"]==0
    assert samsung["by_shares"]["10"]["roundtrip_same_snapshot_bps"]["median"]>20
    assert sha256((out/"cost_sensitivity.json").read_bytes()).hexdigest()==(
        out/"cost_sensitivity.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="exists"):
        analyze(l2_roots=[src],pit_dir=pit,output_dir=out)


def test_insufficient_l2_depth_returns_null_not_zero_or_full_fill(tmp_path):
    src=make_l2(tmp_path,askq=1,bidq=1)
    result=analyze(l2_roots=[src],output_dir=tmp_path/"report")
    sam=result["per_snapshot"][0]
    failed=sam["by_shares"]["1000"]
    assert failed["BUY"]["displayed_depth_sufficient"] is False
    assert failed["SELL"]["displayed_depth_sufficient"] is False
    assert failed["BUY"]["midpoint_adverse_bps"] is None
    assert failed["same_snapshot_hypothetical_roundtrip_bps"] is None


def test_raw_byte_tampering_fails_without_written_report(tmp_path):
    src=make_l2(tmp_path)
    file=src/"raw-000-005930.json"
    file.write_bytes(file.read_bytes()+b"FORGED")
    with pytest.raises(VenueProbeError,match="checksum"):
        analyze(l2_roots=[src],output_dir=tmp_path/"report")
    assert not (tmp_path/"report").exists()


def test_rehashed_derived_quote_does_not_override_raw_provider(tmp_path):
    src=make_l2(tmp_path)
    file=src/"quote-000-005930.json"
    d=json.loads(file.read_bytes())
    d["best_ask"]+=500
    altered=(json.dumps(d,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
    file.write_bytes(altered)
    manifest=json.loads((src/"manifest.json").read_text())
    next(row for row in manifest["quotes"] if row["symbol"]=="005930")[
        "receipt_sha256"]=sha256(altered).hexdigest()
    _save(src,"manifest",manifest)
    with pytest.raises(VenueProbeError,match="differs from raw"):
        analyze(l2_roots=[src],output_dir=tmp_path/"result")


def test_same_day_quote_and_pit_must_not_be_treated_as_verified_fills(tmp_path):
    src=make_l2(tmp_path)
    pit=make_pit(tmp_path,last="2026-10-28")
    with pytest.raises(VenueProbeError,match="No PIT order"):
        analyze(l2_roots=[src],pit_dir=pit,output_dir=tmp_path/"result")


def test_verified_pilot_books_are_costed_only_after_closeout(tmp_path,monkeypatch):
    import quantlab.intraday_krx_dynamic_cost as module
    root=tmp_path/"joint"
    root.mkdir()
    windows=("morning","midday","afternoon")
    window_samples={}
    rows=[]
    for window in windows:
        source=root/f"2026-10-29_{window}_raw"
        source.mkdir()
        manifest=[]
        for index in (0,1):
            for sy in SYMBOLS:
                raw=_bytes(sy)
                # Make the synthetic provider's book clock a real Oct-29 time.
                source_body=json.loads(raw)
                h={"morning":"094339","midday":"123539","afternoon":"143539"}[window]
                source_body["output1"]["aspr_acpt_hour"]=h
                raw=json.dumps(source_body).encode()
                t=datetime.fromisoformat(
                    f"2026-10-29T{h[:2]}:{h[2:4]}:40+09:00")
                q=parse_quote(raw,sy,t)
                norm=(json.dumps(q,sort_keys=True)+"\n").encode()
                base=f"{index:03d}-{sy}-book"
                (source/f"raw-{base}.json").write_bytes(raw)
                (source/f"normalized-{base}.json").write_bytes(norm)
                manifest.append({
                    "sample_index":index,"symbol":sy,"kind":"book",
                    "raw_file":f"raw-{base}.json",
                    "normalized_file":f"normalized-{base}.json",
                    "raw_sha256":sha256(raw).hexdigest(),
                    "normalized_sha256":sha256(norm).hexdigest(),
                    "response_received_utc":t.astimezone(timezone.utc).isoformat(),
                })
        # Three-source records contain another 8 non-book receipts in addition.
        for index in (0,1):
            for sy in SYMBOLS:
                for kind in ("ticks","minute"):
                    manifest.append({"kind":kind,"sample_index":index,"symbol":sy})
        assert len(manifest)==12
        _save(source,"manifest",{"snapshots":manifest})
        checked={
            "window":window,
            "status":"VERIFIED_RESEARCH_ONLY",
            "verified_triplets":4,
            "matched_ticks":10,
            "price_within_candle_range":10,
            "broker_orders":0,
        }
        rows.append(checked)
        window_samples[window]=checked
    final=root/"2026-10-29_final_review"
    _save(final,"final_review",{
        "session":"2026-10-29",
        "status":"ALL_THREE_WINDOWS_VERIFIED_RESEARCH_ONLY",
        "paper_or_live_approval":False,"broker_orders":0,
        "historical_strategy_returns":None,
        "expected_windows":3,
        "verified_windows":3,
        "missing_or_invalid_windows":0,
        "source_verified_windows_not_cross_source_price_certification":True,
        "cross_source_price_consistency_observed_only":
            "ALL_OBSERVED_MATCHES_WITHIN_CANDLE_RANGE_NOT_CERTIFIED",
        "trade_day_independently_certified":False,
        "minute_label_start_end_semantics_independently_certified":False,
        "safe_to_calibrate_historical_strategy_fills":False,
        "window_results":rows,
    })
    monkeypatch.setattr(module,"_verify_window",lambda r,w,**kwargs:
                        (window_samples[w],{"source_verified":True}))
    data=module.analyze(pilot_root=root,output_dir=tmp_path/"report")
    assert data["snapshot_count"]==12
    assert set(data["by_sample_group"])=={
        "2026-10-29_morning","2026-10-29_midday","2026-10-29_afternoon"}
    assert all(data["by_sample_group"][w][sy]["quote_count"]==2
               for w in data["by_sample_group"] for sy in SYMBOLS)
    assert data["actual_broker_fill_records"]==0
    assert data["strategy_returns"] is None
    market_source=next(x for x in data["sources"] if x["kind"]=="joint_pilot")
    assert market_source["trade_day_independently_certified"] is False
    assert market_source["market_observations_are_not_historical_fill_calibration"]
    # Mutating raw quote bytes after finalization invalidates the downstream cost.
    path=root/"2026-10-29_morning_raw"/"raw-000-005930-book.json"
    path.write_bytes(path.read_bytes()+b"t")
    with pytest.raises(VenueProbeError,match="Tampered"):
        module.analyze(pilot_root=root,output_dir=tmp_path/"invalid")
    assert not (tmp_path/"invalid").exists()

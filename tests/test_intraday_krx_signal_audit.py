"""End-to-end immutable signal inventory regression with synthetic KRX minutes."""
from __future__ import annotations
from datetime import date, timedelta
from hashlib import sha256
import json
import pytest
import quantlab.intraday_krx_halt_5m as source_mod
from quantlab.intraday_krx_5m_preview import CLOCKS
from quantlab.intraday_krx_signal_audit import audit
from quantlab.intraday_kis_venues import VenueProbeError

def make_source(tmp_path, monkeypatch):
    raw=tmp_path/"raw"
    dates=[]
    d=date(2026,9,1)
    while len(dates)<12:
        if d.weekday()<5:
            dates.append(d.isoformat())
        d+=timedelta(days=1)
    rows={}
    for idx,session in enumerate(dates):
        records={}
        for j,clock in enumerate(CLOCKS):
            if idx==5 and clock=="091400":
                continue
            # day 11 unusually high opening volume and upward movement
            price=102 if idx==11 and j>=6 else 100
            vol=200 if idx==11 and j<30 else 100
            records[clock]={"stck_bsop_date":session.replace("-",""),
                "stck_cntg_hour":clock, "stck_oprc":str(price),
                "stck_hgpr":str(price+1), "stck_lwpr":str(price),
                "stck_prpr":str(price), "cntg_vol":str(vol)}
        records["153000"]={"stck_bsop_date":session.replace("-",""),
            "stck_cntg_hour":"153000", "stck_oprc":"100",
            "stck_hgpr":"100","stck_lwpr":"100","stck_prpr":"100",
            "cntg_vol":"0"}
        rows[session]=records
        (raw/f"KIS_J_EXT_005930_{session}").mkdir(parents=True)
    def fake(root,symbol,session,market):
        assert root==raw and symbol=="005930" and market=="J"
        return {"source_minute_bar_sha256":"a"*64}, rows[session]
    monkeypatch.setattr(source_mod,"_read_one",fake)
    folder=tmp_path/"dataset"
    source_mod.build(source_root=raw,out_dir=folder,
                     symbols=("005930",),sessions=tuple(dates))
    return folder,dates

def test_real_halt_dataset_path_keeps_invalid_day_and_never_makes_pnl(tmp_path,monkeypatch):
    folder,days=make_source(tmp_path,monkeypatch)
    result=audit(folder,tmp_path/"report")
    assert result["stock_days_preserved"]==12
    assert len(result["day_records"])==12
    assert result["by_symbol"]["005930"]["blocked_days"]==1
    assert result["day_records"][5]["signal_observation_allowed"] is False
    assert result["day_records"][5]["signal_names_not_trades"]==[]
    assert result["by_symbol"]["005930"]["candidate_full_days"]==11
    assert result["by_symbol"]["005930"]["fixed_signal_counts_by_window"][
        "untouched_test"]["intraday_momentum_30m"]==1
    assert all(x["strategy_return"] is None for x in result["day_records"])
    assert result["trade_or_fill_simulated"] is False
    assert result["oos_returns_computed"] is False
    assert result["usable_for_backtesting"] is False
    assert result["usable_for_paper_or_live"] is False
    p=tmp_path/"report"/"signal_audit.json"
    assert sha256(p.read_bytes()).hexdigest()==(
        tmp_path/"report"/"signal_audit.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already exists"):
        audit(folder,tmp_path/"report")

def test_sha_tampering_and_adversarial_order_permission_fail_closed(tmp_path,monkeypatch):
    folder,_=make_source(tmp_path,monkeypatch)
    f=folder/"005930_halt_5m_UNVERIFIED.json"
    data=json.loads(f.read_bytes())
    data[0]["slots"][0]["continuous_order_execution_allowed"]=True
    body=(json.dumps(data,sort_keys=True)+"\n").encode()
    f.write_bytes(body)
    (folder/"005930_halt_5m_UNVERIFIED.sha256").write_text(
        sha256(body).hexdigest()+"\n")
    with pytest.raises(VenueProbeError,match="live order eligibility"):
        audit(folder,tmp_path/"report")
    assert not (tmp_path/"report").exists()

def test_summary_checksum_tampering_fails_before_output(tmp_path,monkeypatch):
    folder,_=make_source(tmp_path,monkeypatch)
    f=folder/"halt_5m_summary.json"
    f.write_bytes(f.read_bytes()+b"TAMPER")
    with pytest.raises(VenueProbeError,match="checksum"):
        audit(folder,tmp_path/"report")
    assert not (tmp_path/"report").exists()

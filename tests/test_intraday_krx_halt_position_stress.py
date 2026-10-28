"""Strict non-PnL held-position stress tests for KRX halt risk."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

import quantlab.intraday_krx_halt_5m as source_mod
from quantlab.intraday_krx_5m_preview import CLOCKS
from quantlab.intraday_krx_halt_position_stress import replay, main
from quantlab.intraday_kis_venues import VenueProbeError


def _fixture(tmp_path: Path, monkeypatch, *, post_price: int = 103,
             no_resumption: bool = False):
    folder=tmp_path/"mock_sources"
    halt="2026-07-28"
    other="2026-10-23"
    sessions=(halt,other)
    rows={}
    for session in sessions:
        each={}
        for clock in CLOCKS:
            if session==halt and (
                "101400"<=clock<="104300" or
                (no_resumption and clock>="104500")
            ):
                continue
            price=post_price if session==halt and clock>="104500" else 100
            each[clock]={
                "stck_bsop_date":session.replace("-",""),
                "stck_cntg_hour":clock,
                "stck_oprc":str(price),
                "stck_hgpr":str(price),
                "stck_lwpr":str(price),
                "stck_prpr":str(price),
                "cntg_vol":"100",
            }
        each["153000"]={
            "stck_bsop_date":session.replace("-",""),
            "stck_cntg_hour":"153000",
            "stck_oprc":"100","stck_hgpr":"100",
            "stck_lwpr":"100","stck_prpr":"100","cntg_vol":"100",
        }
        rows[session]=each
        (folder/f"KIS_J_EXT_005930_{session}").mkdir(parents=True)
    def read(root,symbol,session,market):
        assert market=="J"
        assert root==folder
        return {"source_minute_bar_sha256":"b"*64},rows[session]
    monkeypatch.setattr(source_mod,"_read_one",read)
    storage=tmp_path/"halt_5m_sources"
    source_mod.build(source_root=folder,out_dir=storage,
                     symbols=("005930",),sessions=sessions)
    return storage


def test_hypothetical_exit_fails_but_existing_position_remains(tmp_path,monkeypatch):
    root=_fixture(tmp_path,monkeypatch)
    report=replay(input_root=root,out_dir=tmp_path/"risk",assumed_held_shares=25)
    assert report["source_stock_days"]==2
    assert report["scenario_count"]==1
    assert report["all_source_days_preserved"] is True
    assert report["usable_for_backtesting"] is False
    assert report["cash_or_equity_curve_computed"] is False
    assert report["sharpe_or_mdd_computed"] is False
    risk=report["halt_exposure_scenarios"][0]
    assert risk["hypothetical_initial_held_shares"]==25
    assert risk["hypothetical_final_held_shares"]==25
    assert risk["hypothetical_exit_order_at_activation"]["filled_shares"]==0
    assert risk["hypothetical_exit_order_at_activation"]["order_left_queued"] is False
    assert risk["hypothetical_exit_order_at_activation"]["assumed_position_liquidated"] is False
    assert risk["post_halt_entry_or_exit"]["filled_shares"]==0
    assert risk["first_complete_5m_after_reported_blackout_open"]==103
    assert risk["last_complete_5m_before_halt_close"]==100
    assert risk["observed_cross_gap_price_change_fraction_not_a_fill_or_pnl"]==pytest.approx(.03)
    assert report["market_halt_days_not_excluded_from_original_test_population"]
    blob=(tmp_path/"risk/stress_report.json").read_bytes()
    assert sha256(blob).hexdigest()==(
        tmp_path/"risk/stress_report.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="already exists"):
        replay(input_root=root,out_dir=tmp_path/"risk",assumed_held_shares=10)


def test_no_posthalt_full_bar_does_not_invent_exit_or_gap(tmp_path,monkeypatch):
    root=_fixture(tmp_path,monkeypatch,no_resumption=True)
    result=replay(input_root=root,out_dir=tmp_path/"risk")
    episode=result["halt_exposure_scenarios"][0]
    assert episode["first_complete_5m_after_reported_blackout_clock"] is None
    assert episode["observed_cross_gap_price_change_fraction_not_a_fill_or_pnl"] is None
    assert episode["hypothetical_final_held_shares"]==10
    assert episode["can_estimate_executable_exit_price"] is False


def test_missing_or_tampered_source_fail_closed(tmp_path,monkeypatch):
    root=_fixture(tmp_path,monkeypatch)
    file=root/"005930_halt_5m_UNVERIFIED.json"
    file.write_bytes(file.read_bytes()+b"TAMPER")
    with pytest.raises(VenueProbeError,match="checksum"):
        replay(input_root=root,out_dir=tmp_path/"risk")
    assert not (tmp_path/"risk").exists()


@pytest.mark.parametrize("shares",[-1,100001,True,1.1])
def test_unbounded_or_invalid_holdings_not_accepted(tmp_path,shares):
    with pytest.raises(VenueProbeError,match="hypothetical|bounded|shares|nonnegative"):
        replay(input_root=tmp_path,out_dir=tmp_path/"out",assumed_held_shares=shares)
    assert not (tmp_path/"out").exists()


def test_adversarial_edited_unverified_source_cannot_authorize_fills(
    tmp_path,monkeypatch
):
    root=_fixture(tmp_path,monkeypatch)
    path=root/"005930_halt_5m_UNVERIFIED.json"
    records=json.loads(path.read_bytes())
    # Attacker modifies the state in an input and recomputes its hash.
    records[0]["slots"][0]["continuous_order_execution_allowed"]=True
    new=(json.dumps(records,sort_keys=True)+"\n").encode()
    path.write_bytes(new)
    (root/"005930_halt_5m_UNVERIFIED.sha256").write_text(
        sha256(new).hexdigest()+"\n")
    with pytest.raises(VenueProbeError,match="live order eligibility"):
        replay(input_root=root,out_dir=tmp_path/"risk")
    assert not (tmp_path/"risk").exists()


def test_fake_circuit_breaker_day_must_match_expected_report(tmp_path,monkeypatch):
    root=_fixture(tmp_path,monkeypatch)
    path=root/"005930_halt_5m_UNVERIFIED.json"
    rows=json.loads(path.read_bytes())
    rows[0]["reported_cb"]=None
    blob=(json.dumps(rows,sort_keys=True)+"\n").encode()
    path.write_bytes(blob)
    (root/"005930_halt_5m_UNVERIFIED.sha256").write_text(
        sha256(blob).hexdigest()+"\n")
    with pytest.raises(VenueProbeError,match="identity or external event"):
        replay(input_root=root,out_dir=tmp_path/"risk")


def test_cli_fails_without_source_and_never_creates_output(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["--input-root",str(tmp_path/"none"),"--out-dir",str(tmp_path/"out")])
    assert exc.value.code==2
    assert not (tmp_path/"out").exists()

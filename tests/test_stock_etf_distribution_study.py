"""ETF ex-distribution hypothesis is research-only and never rewrites OHLC."""
from __future__ import annotations

from hashlib import sha256
import json

import pytest

from quantlab import stock_etf_distribution_study as m


def _gap():
    vals={
        "2026-10-22":(
            [88805.,90120.,85790.,86525.],
            [88678.,89991.,85668.,86401.]),
        "2026-10-26":(
            [86265.,87540.,85700.,85995.],
            [86142.,87415.,85578.,85872.]),
        "2026-10-27":(
            [88920.,90760.,88625.,88905.],
            [88793.,90631.,88499.,88779.]),
        "2026-10-28":(
            [91315.,95990.,90855.,95650.],
            [91186.,95854.,90726.,95515.]),
    }
    rows=[]
    for day,(observed,vendor) in vals.items():
        rows.append({
            "market":"KRX","symbol":"069500","session":day,
            "latest_conflict_classification":
                "COHERENT_OHLC_SCALE_SHIFT_POSSIBLE_ADJUSTMENT_NOT_VERIFIED",
            "material_price_conflict_unresolved":True,
            "earlier_agreeing_price_snapshot_seen":day=="2026-10-22",
            "sources":[{
                "raw_sha256":sha256(("069500"+day).encode()).hexdigest(),
                "observed_ohlc":observed,"vendor_ohlc":vendor,
            }],
        })
    other=[("AAPL","2026-10-22","SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED"),
           ("AAPL","2026-10-27","SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED"),
           ("MSFT","2026-10-22","MATERIAL_OHLC_PRICE_CONFLICT"),
           ("MSFT","2026-10-27","SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED"),
           ("SPY","2026-10-27","SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED")]
    for sym,day,kind in other:
        rows.append({"market":"NASDAQ_NYSE","symbol":sym,"session":day,
                     "latest_conflict_classification":kind,
                     "sources":[{
                         "raw_sha256":sha256((sym+day).encode()).hexdigest()
                     }]})
    return {
        "status":"VENDOR_HISTORY_GAP_PROVENANCE_UNRESOLVED_NO_TRADING",
        "broker_orders":0,
        "paper_or_live_trading_eligible":False,
        "raw_source_sha256_checked_for_every_conflict_variant":True,
        "missing_days_are_not_automatically_backfilled":True,
        "gaps":rows,
    }


def test_issuer_distribution_factor_fits_all_16_vendor_OHLC_without_certifying_prices():
    study=m.study(_gap(),"a"*64)
    assert study["status"]==m.STATUS
    assert study["total_etf_gap_sessions_with_consistent_factor"]==4
    assert study["etf_ohlc_observations_compared"]==16
    assert study["max_abs_etf_ohlc_residual_krw"]<=1.01
    assert study["issuer_distribution_evidence"]["cash_distribution_per_unit_krw"]==136
    assert study["issuer_distribution_evidence"]["reference_observed_close_krw"]==95650
    assert study["issuer_distribution_evidence"]["hypothetical_multiplier"]==(
        95650-136)/95650
    assert study["etf_vendor_algorithm_independently_confirmed"] is False
    assert study["etf_historical_executable_price_certified"] is False
    assert study["us_missing_days_not_explained_by_etf_distribution"]==5
    assert study["overall_unresolved_missing_stock_sessions"]==9
    assert study["missing_days_replaced_in_long_history"]==0
    assert study["broker_orders"]==0
    assert study["paper_or_live_trading_eligible"] is False
    assert study["certified_strategy_returns"] is None
    assert all(not row["eligible_for_history_rewrite"]
               for row in study["etf_comparisons"])
    assert all(not row["eligible_for_history_rewrite"]
               for row in study["us_unresolved"])


def test_wrong_cash_distribution_fails_and_does_not_auto_adjust(monkeypatch):
    monkeypatch.setattr(m,"DISTRIBUTION_KRW",155)
    with pytest.raises(ValueError,match="does not explain"):
        m.study(_gap(),"b"*64)


def test_missing_etf_day_or_wrong_reference_close_refused():
    evidence=_gap()
    evidence["gaps"]=[v for v in evidence["gaps"] if v["session"]!="2026-10-22"]
    with pytest.raises(ValueError,match="Expected four"):
        m.study(evidence,"c"*64)
    evidence=_gap()
    item=next(r for r in evidence["gaps"] if r["session"]=="2026-10-28")
    item["sources"][0]["observed_ohlc"][3]+=10
    with pytest.raises(ValueError,match="Unexpected pre-distribution"):
        m.study(evidence,"d"*64)


def test_unexplained_us_material_gap_must_remain_unresolved():
    evidence=_gap()
    study=m.study(evidence,"c"*64)
    msft=next(row for row in study["us_unresolved"]
              if row["symbol"]=="MSFT" and row["date"]=="2026-10-22")
    assert msft["classification"]=="MATERIAL_OHLC_PRICE_CONFLICT"
    assert msft["requires_independent_trade_price_reference"] is True


def test_report_pin_and_independent_immutable_readonly_audit(tmp_path,monkeypatch):
    root=tmp_path/"stock_daily"
    root.mkdir()
    a=root/"observations.sqlite3"
    b=root/"history.sqlite3"
    a.write_bytes(b"fake-source-not-mutated")
    b.write_bytes(b"fake-history-not-mutated")
    report=_gap()
    src=tmp_path/"gap.json"
    data=(json.dumps(report,sort_keys=True)+"\n").encode()
    src.write_bytes(data)
    src.with_suffix(".json.sha256").write_text(sha256(data).hexdigest()+"\n")
    def inspect(*,observations,history,since):
        assert str(observations)==str(a)
        assert str(history)==str(b)
        assert since=="2026-10-20"
        return report
    monkeypatch.setattr(m,"inspect",inspect)
    out=tmp_path/"out"
    result=m.analyze(gap_report=src,observations=a,history=b,output_dir=out)
    assert result["source_gap_report_reindependently_verified_against_readonly_stock_dbs"]
    assert result["source_gap_provenance_sha256"]==sha256(data).hexdigest()
    blob=(out/"study.json").read_bytes()
    assert sha256(blob).hexdigest()==(out/"study.sha256").read_text().strip()
    assert (out/"study.json").stat().st_mode&0o777==0o600
    assert (out/"study.sha256").stat().st_mode&0o777==0o600
    assert a.read_bytes()==b"fake-source-not-mutated"
    assert b.read_bytes()==b"fake-history-not-mutated"
    with pytest.raises(ValueError,match="already exists"):
        m.analyze(gap_report=src,observations=a,history=b,output_dir=out)
    src.write_bytes(data+b"forged")
    with pytest.raises(ValueError,match="SHA mismatch"):
        m.analyze(gap_report=src,observations=a,history=b,
                  output_dir=tmp_path/"other")


def test_report_with_rehashed_but_changed_content_rejected_by_original_DB_audit(
        tmp_path,monkeypatch):
    root=tmp_path/"stock_daily"
    root.mkdir()
    a=root/"observations.sqlite3";b=root/"history.sqlite3"
    a.write_bytes(b"evidence-a");b.write_bytes(b"evidence-b")
    report=_gap()
    modified={**report,"broker_orders":1}
    src=tmp_path/"gap.json"
    data=(json.dumps(modified,sort_keys=True)+"\n").encode()
    src.write_bytes(data)
    src.with_suffix(".json.sha256").write_text(sha256(data).hexdigest()+"\n")
    monkeypatch.setattr(m,"inspect",lambda **_:report)
    with pytest.raises(ValueError,match="not identical"):
        m.analyze(gap_report=src,observations=a,history=b,
                  output_dir=tmp_path/"out")

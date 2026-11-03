"""Offline test of the independent public-quote price-basis sensitivity study."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab import stock_price_basis_sensitivity as m
from quantlab.stock_etf_distribution_study import study
from quantlab.stock_price_variant_dataset import _create_rows
from quantlab.stock_us_public_price_audit import _rows, _cent
from test_stock_us_public_price_audit import _gap as us_gap
from test_stock_etf_distribution_study import _gap as etf_gap


def _quodd_html(open_price="533.140",high="533.596"):
    return (
        "<html><head><title>MSFT daily price history</title></head><body>"
        "<table><tr><th>Date</th><th>Open</th><th>High</th><th>Low</th>"
        "<th>Close</th><th>Volume</th></tr>"
        "<tr><td>10/22/2026</td><td>"+open_price+"</td><td>"+high+
        "</td><td>520.300</td><td>521.080</td><td>21,266,610</td></tr>"
        "</table></body></html>"
    ).encode()


def _source_reports():
    gap=us_gap()
    site={}
    for rec in gap["gaps"]:
        sy=rec["symbol"]
        vals=[_cent(x) for x in rec["sources"][0]["vendor_ohlc"]]
        for prov in ("stockanalysis","yahoo"):
            item=site.setdefault((prov,sy),{"days":{}})
            item["days"][rec["session"]]=vals
    us={"status":"US_STOCK_PUBLIC_PRICE_COMPARISON_ONLY_NO_EXCHANGE_EXECUTION_CERTIFICATION",
        "broker_orders":0,"backtest_paper_live_approved":False,
        "checked_conflict_sessions":5,"original_market_price_corrections_committed":0,
        "comparisons":_rows(gap,site)}
    etf_study=study(etf_gap(),"e"*64)
    etf_rows,us_rows=_create_rows(etf_study)
    etf_manifest={"broker_orders":0,"approved_for_backtest":False,
                  "etf_sessions":4,"us_unresolved_sessions":5}
    return us,etf_manifest,etf_rows


def _fixture(tmp_path,monkeypatch):
    us,etf,etf_rows=_source_reports()
    us_root=tmp_path/"us"
    etf_root=tmp_path/"etf"
    study_root=tmp_path/"study"
    for d in (us_root,etf_root,study_root):
        d.mkdir()
    (us_root/"report.json").write_text('{"source":"US"}')
    (us_root/"manifest.json").write_text('{"source":"US"}')
    (etf_root/"manifest.json").write_text('{"source":"ETF"}')
    (etf_root/"etf_variants.jsonl").write_text("".join(
        json.dumps(x)+"\n" for x in etf_rows))
    (study_root/"study.json").write_text('{"source":"original-study"}')
    gap=tmp_path/"gap.json"
    gap.write_text('{"source":"pinned-gap"}')
    obs=tmp_path/"observations.sqlite3"
    hist=tmp_path/"history.sqlite3"
    obs.write_bytes(b"immutable-observation")
    hist.write_bytes(b"immutable-history")
    monkeypatch.setattr(m,"_parents",lambda **kw:(us,etf))
    kw={
        "root":tmp_path/"out","us_root":us_root,
        "etf_root":etf_root,"study_dir":study_root,"gap_report":gap,
        "observations":obs,"history":hist,
    }
    class Response:
        status_code=200
        content=_quodd_html()
    def getter(url,**args):
        assert url==m.QUODD_URL
        assert args["allow_redirects"] is False
        assert not any(k.lower() in ("authorization","api_key") for k in args["headers"])
        return Response()
    return kw,getter


def test_precise_public_source_is_mixed_per_ohlc_field():
    raw=_quodd_html()
    result=m._parse_quodd(raw)
    assert result["ohlc_strings"]==["533.140","533.596","520.300","521.080"]
    assert result["volume_string"]=="21266610"
    assert result["not_official_exchange_auction_reference"] is True


def test_report_5_us_4_etf_distinct_price_scenarios(tmp_path,monkeypatch):
    kw,getter=_fixture(tmp_path,monkeypatch)
    obs=kw["observations"].read_bytes()
    hist=kw["history"].read_bytes()
    report=m.collect(**kw,getter=getter)
    assert report["status"]==m.STATUS
    assert report["us_count"]==5 and report["etf_count"]==4
    assert report["msft_open_first_observed_disagreement_usd"]==pytest.approx(.44,abs=.0001)
    assert report["absolute_max_us_open_disagreement_bps"]==pytest.approx(8.25985,abs=.002)
    assert report["strategy_pnl"] is None
    assert report["strategy_sharpe"] is None
    assert report["broker_orders"]==0
    assert report["backtest_or_live_approved"] is False
    msft=next(r for r in report["us_bar_sensitivity"]
              if (r["symbol"],r["session"])==("MSFT","2026-10-22"))
    assert msft["later_minus_original_open_close_move_bps"]==pytest.approx(-8.073,abs=.002)
    assert msft["additional_third_party_price_precision_source"][
        "open_matches_later_vendor_to_0_0001_usd"]
    assert msft["additional_third_party_price_precision_source"][
        "high_matches_initial_observed_to_0_0001_usd"]
    assert msft["additional_third_party_price_precision_source"][
        "does_not_select_a_whole_candle_source_as_canonical"]
    for day in report["etf_bar_sensitivity"]:
        hypothesis=day["price_basis_scenarios"]["issuer_cash_distribution_ratio_hypothesis"]
        assert hypothesis["close_level_difference_from_original_bps"]==pytest.approx(-14.2185,abs=.001)
        assert hypothesis["open_close_arithmetic_move_difference_from_original_bps"]==pytest.approx(0,abs=1e-7)
        assert day["cross_day_price_splicing_prohibited"] is True
    assert m.verify(**kw)==report
    assert kw["observations"].read_bytes()==obs
    assert kw["history"].read_bytes()==hist
    assert kw["root"].stat().st_mode & 0o777==0o700
    assert all(p.stat().st_mode&0o777==0o600 for p in kw["root"].iterdir())


def test_html_tampering_fail_closed(tmp_path,monkeypatch):
    kw,getter=_fixture(tmp_path,monkeypatch)
    m.collect(**kw,getter=getter)
    path=kw["root"]/m.FILENAME
    path.write_bytes(path.read_bytes().replace(b"533.140",b"533.180"))
    with pytest.raises(ValueError,match="does not match"):
        m.verify(**kw)


def test_manifest_rehash_and_fake_trading_approval_is_rejected(tmp_path,monkeypatch):
    kw,getter=_fixture(tmp_path,monkeypatch)
    m.collect(**kw,getter=getter)
    path=kw["root"]/"manifest.json"
    data=json.loads(path.read_text())
    data["backtest_or_live_approved"]=True
    b=(json.dumps(data,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
    path.write_bytes(b)
    (kw["root"]/"manifest.sha256").write_text(sha256(b).hexdigest()+"\n")
    with pytest.raises(ValueError,match="manifest changed"):
        m.verify(**kw)


def test_original_immutable_etf_variant_mutation_stops_reverify(tmp_path,monkeypatch):
    kw,getter=_fixture(tmp_path,monkeypatch)
    m.collect(**kw,getter=getter)
    p=kw["etf_root"]/"etf_variants.jsonl"
    p.write_bytes(p.read_bytes().replace(b"95650",b"95000"))
    with pytest.raises(ValueError,match="hypothesis not reproduced"):
        # This additionally tests the arithmetic cross-check even when
        # only the fixture, rather than the actual parent verifier, is used.
        m._diagnose(_source_reports()[0],_source_reports()[1],
                    kw["etf_root"],m._parse_quodd(_quodd_html()))


def test_existing_output_no_remote_request(tmp_path,monkeypatch):
    kw,getter=_fixture(tmp_path,monkeypatch)
    m.collect(**kw,getter=getter)
    def fail(*a,**k):
        raise AssertionError("Existing research output must not call network")
    with pytest.raises(ValueError,match="already exists"):
        m.collect(**kw,getter=fail)


def test_source_reference_inconsistent_is_fail_closed(tmp_path,monkeypatch):
    kw,getter=_fixture(tmp_path,monkeypatch)
    class Response:
        status_code=200
        content=_quodd_html(open_price="532.700")
    with pytest.raises(ValueError,match="mixed-source"):
        m.collect(**kw,getter=lambda *a,**k:Response())
    assert not kw["root"].exists()


def test_invalid_open_high_candle_refused():
    with pytest.raises(ValueError,match="Invalid source OHLC range"):
        m._parse_quodd(_quodd_html(high="533.130"))
    with pytest.raises(ValueError,match="precision invalid"):
        m._parse_quodd(_quodd_html(open_price="533.1401"))


def test_arithmetical_move_is_not_actual_trade_result():
    price=m._to_prices([533.14,533.596,520.30,521.08])
    metrics=m._bar_metrics(price)
    assert metrics["open_to_close_arithmetic_bps"]<0
    assert "strategy_pnl" not in metrics
    assert "filled_order_returns" not in metrics

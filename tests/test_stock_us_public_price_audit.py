"""Offline regression for six real-HTML-shaped US research quote captures."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from quantlab import stock_us_public_price_audit as m


def _gap():
    samples={
        ("AAPL","2026-10-22"):(
            [344.93499755859375,348.3599853515625,342.2799987792969,348.2200012207031],
            [344.94000244140625,348.3599853515625,342.2799987792969,348.2200012207031]),
        ("AAPL","2026-10-27"):(
            [343.1650085449219,347.7799987792969,342.3900146484375,347.1000061035156],
            [343.1700134277344,347.7799987792969,342.3900146484375,347.1000061035156]),
        ("MSFT","2026-10-22"):(
            [532.7000122070312,533.5960083007812,520.2999877929688,521.0800170898438],
            [533.1400146484375,533.5999755859375,520.2999877929688,521.0800170898438]),
        ("MSFT","2026-10-27"):(
            [532.3499755859375,534.7000122070312,528.3309936523438,531.3300170898438],
            [532.3499755859375,534.7000122070312,528.3300170898438,531.3300170898438]),
        ("SPY","2026-10-27"):(
            [767.3400268554688,771.1300048828125,765.2100219726562,769.6599731445312],
            [767.3400268554688,771.1400146484375,765.2100219726562,769.6599731445312]),
    }
    return {
        "status":"VENDOR_HISTORY_GAP_PROVENANCE_UNRESOLVED_NO_TRADING",
        "broker_orders":0,
        "paper_or_live_trading_eligible":False,
        "gaps":[{
            "market":"NASDAQ_NYSE","symbol":sym,"session":day,
            "latest_conflict_classification":(
                "MATERIAL_OHLC_PRICE_CONFLICT" if (sym,day)==("MSFT","2026-10-22")
                else "SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED"),
            "sources":[{
                "observed_ohlc":old,"vendor_ohlc":new,
                "raw_sha256":sha256((sym+day).encode()).hexdigest(),
            }],
        } for (sym,day),(old,new) in samples.items()],
    }


def _html(sy,provider):
    rows=[]
    for gap in _gap()["gaps"]:
        if gap["symbol"]!=sy:
            continue
        day=datetime.strptime(gap["session"],"%Y-%m-%d")
        date_text=f"{day.strftime('%b')} {day.day}, {day.year}"
        cells=[date_text,*[m._cent(x) for x in gap["sources"][0]["vendor_ohlc"]]]
        if provider=="stockanalysis":
            cells.extend(["0.00","0.00%","20,000,000"])
        else:
            cells.extend(["0.00","20,000,000"])
        rows.append("<tr>"+ "".join(f"<td><span>{v}</span></td>" for v in cells)+"</tr>")
    headers=["Date","Open","High","Low","Close"]
    if provider=="stockanalysis":
        headers+=["Adj. Close","Change","Volume"]
    else:
        headers+=["Adj Close","Volume"]
    html=("<!DOCTYPE html><html><head><title>"+sy+" public historical prices</title></head>"
          "<body><section>"+("historical price table "+sy+" ")*12+"</section>"
          "<table><thead><tr>"+ "".join(f"<th>{x}</th>" for x in headers)+
          "</tr></thead><tbody>"+ "".join(rows)+"</tbody></table></body></html>")
    assert len(html.encode())>300
    return html.encode()


def _setup(tmp_path,monkeypatch):
    gap=_gap()
    monkeypatch.setattr(m,"_pinned_gap",lambda p,o,h:(gap,"f"*64))
    captures=[]
    class Response:
        status_code=200
        def __init__(self,content):self.content=content
    def getter(url,headers,timeout,allow_redirects):
        assert not allow_redirects
        assert "Authorization" not in headers
        key=next(pair for pair,ref in m.SOURCE_URLS.items() if ref==url)
        captures.append(key)
        return Response(_html(key[1],key[0]))
    args={
        "root":tmp_path/"capture",
        "gap_report":tmp_path/"stock_daily/gap.json",
        "observations":tmp_path/"stock_daily/observations.sqlite3",
        "history":tmp_path/"stock_daily/history.sqlite3",
    }
    return args,getter,captures


def test_six_site_snapshots_match_all_five_vendor_display_prices(tmp_path,monkeypatch):
    args,getter,calls=_setup(tmp_path,monkeypatch)
    result=m.collect(**args,getter=getter,
                     clock=lambda:datetime(2026,11,3,1,50,tzinfo=timezone.utc))
    assert len(calls)==6 and len(set(calls))==6
    assert result["status"]==m.STATUS
    assert result["checked_conflict_sessions"]==5
    assert result["public_sites_agree_at_displayed_cents"]==5
    assert result["later_vendor_matches_both_displayed_cents"]==5
    assert result["exchange_grade_executions_verified"] is False
    assert result["historical_market_data_complete"] is False
    assert result["broker_orders"]==0
    assert result["backtest_paper_live_approved"] is False
    assert m.verify(**args)==result
    assert args["root"].stat().st_mode&0o777==0o700
    assert all(p.stat().st_mode&0o777==0o600 for p in args["root"].iterdir())
    records={(x["symbol"],x["session"]):x for x in result["comparisons"]}
    assert records["MSFT","2026-10-22"]["original_display_price_classification"]==(
        "MATERIAL_ORIGINAL_PRICE_DISAGREEMENT_NOT_RESOLVED")
    assert records["AAPL","2026-10-22"]["original_display_price_classification"]==(
        "ORIGINAL_NEAR_HALF_CENT_TIE_AMBIGUOUS_NOT_RESOLVED")
    assert records["SPY","2026-10-27"]["original_display_price_classification"]==(
        "ORIGINAL_ONE_CENT_PRICE_DISAGREEMENT_NOT_RESOLVED")
    assert records["MSFT","2026-10-27"]["original_display_price_classification"]==(
        "ORIGINAL_MATCHES_PUBLIC_CENTS_PRECISION_NOT_EXCHANGE_CERTIFIED")


def test_tampered_html_fails_even_if_manifest_not_touched(tmp_path,monkeypatch):
    args,getter,_=_setup(tmp_path,monkeypatch)
    m.collect(**args,getter=getter,clock=lambda:datetime.now(timezone.utc))
    path=args["root"]/"yahoo_MSFT.html"
    path.write_bytes(path.read_bytes().replace(b"533.14",b"533.16"))
    with pytest.raises(ValueError,match="source manifest"):
        m.verify(**args)


def test_forged_report_and_rehashed_sha_rejected(tmp_path,monkeypatch):
    args,getter,_=_setup(tmp_path,monkeypatch)
    m.collect(**args,getter=getter,clock=lambda:datetime.now(timezone.utc))
    p=args["root"]/"report.json"
    report=json.loads(p.read_text())
    report["backtest_paper_live_approved"]=True
    raw=(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
    p.write_bytes(raw)
    (args["root"]/"report.sha256").write_text(sha256(raw).hexdigest()+"\n")
    with pytest.raises(ValueError,match="diverges"):
        m.verify(**args)


def test_rerun_never_networks_or_overwrites(tmp_path,monkeypatch):
    args,getter,calls=_setup(tmp_path,monkeypatch)
    m.collect(**args,getter=getter,clock=lambda:datetime.now(timezone.utc))
    assert len(calls)==6
    with pytest.raises(ValueError,match="exists"):
        m.collect(**args,getter=lambda **_:pytest.fail("would call internet"),
                  clock=lambda:datetime.now(timezone.utc))
    assert len(calls)==6


def test_http_errors_or_partial_html_cannot_be_persisted(tmp_path,monkeypatch):
    args,getter,_=_setup(tmp_path,monkeypatch)
    class Response:
        status_code=429
        content=b"rate limited"
    with pytest.raises(ValueError,match="Invalid third-party"):
        m.collect(**args,getter=lambda *a,**k:Response(),
                  clock=lambda:datetime.now(timezone.utc))
    assert not args["root"].exists()


def test_public_cent_conflict_is_not_silently_marked_agreement(tmp_path,monkeypatch):
    args,getter,_=_setup(tmp_path,monkeypatch)
    def altered(url,**kw):
        r=getter(url,**kw)
        if "stockanalysis.com" in url and "msft" in url:
            r.content=r.content.replace(b"533.14",b"533.16")
        return r
    result=m.collect(**args,getter=altered,clock=lambda:datetime.now(timezone.utc))
    assert result["public_sites_agree_at_displayed_cents"]==4
    row=next(x for x in result["comparisons"] if
             (x["symbol"],x["session"])==("MSFT","2026-10-22"))
    assert row["original_display_price_classification"]=="PUBLIC_WEB_PAGES_DISAGREE"
    assert row["price_gap_auto_resolved"] is False
    assert row["model_training_backtest_paper_live_allowed"] is False


def test_invalid_candle_range_fails_source_parsing():
    fake=_html("MSFT","stockanalysis").replace(b"520.30",b"534.00")
    with pytest.raises(ValueError,match="Invalid public OHLC range"):
        m._parse_html(fake,provider="stockanalysis",ticker="MSFT",
                      days=m.SYMBOL_DATES["MSFT"])


def test_unauthorized_symbol_and_unrelated_domain_not_browsed(tmp_path,monkeypatch):
    args,getter,_=_setup(tmp_path,monkeypatch)
    assert set(m.SOURCE_URLS)=={(p,s) for p in m.PROVIDERS for s in m.SYMBOL_DATES}
    with pytest.raises(ValueError,match="Unexpected"):
        m._parse_html(_html("MSFT","yahoo"),provider="yahoo",ticker="NVDA",
                      days=("2026-10-22",))
    bad=_gap()
    bad["gaps"][0]["symbol"]="NVDA"
    with pytest.raises(ValueError,match="Unexpected US gap population"):
        m._rows(bad,{})


def test_captured_source_identity_checked_independent_of_sha(tmp_path,monkeypatch):
    args,getter,_=_setup(tmp_path,monkeypatch)
    m.collect(**args,getter=getter,clock=lambda:datetime.now(timezone.utc))
    p=args["root"]/"stockanalysis_MSFT.html"
    p.write_bytes(p.read_bytes().replace(b"MSFT public historical",b"NVDA public historical"))
    with pytest.raises(ValueError):
        m.verify(**args)

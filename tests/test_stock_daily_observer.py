"""Tests for market-local day cutoff, immutable bars, provider provenance."""
from __future__ import annotations

from datetime import datetime,timezone,timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from quantlab import stock_daily_observer as m

NOW=datetime(2026,10,21,13,0,tzinfo=timezone.utc)
DAY=b'''[['\xeb\x82\xa0\xec\xa7\x9c', '\xec\x8b\x9c\xea\xb0\x80', '\xea\xb3\xa0\xea\xb0\x80', '\xec\xa0\x80\xea\xb0\x80', '\xec\xa2\x85\xea\xb0\x80', '\xea\xb1\xb0\xeb\x9e\x98\xeb\x9f\x89', '\xec\x99\xb8\xea\xb5\xad\xec\x9d\xb8\xec\x86\x8c\xec\xa7\x84\xec\x9c\xa8'], ["20261019", 100, 105, 99, 101, 123, 0], ["20261020", 102, 108, 101, 105, 200, 0], ["20261021", 108, 111, 105, 110, 300, 0]]'''

class Response:
    def __init__(self,content,url):
        self.content=content
        self.url=url
    def raise_for_status(self):pass

def naver(url,**kwargs):
    symbol=kwargs["params"]["symbol"]
    assert symbol in m.KR
    return Response(DAY,"https://api.finance.naver.com/siseJson.naver")

def us_payload(ticker,*,edit=False):
    dates=[datetime(2026,10,d,13,30,tzinfo=timezone.utc) for d in (19,20,21)]
    values=[100.,101.,105. if not edit else 106.]
    return json.dumps({"chart":{"error":None,"result":[{
        "meta":{"symbol":ticker,"currency":"USD","exchangeTimezoneName":"America/New_York"},
        "timestamp":[int(x.timestamp()) for x in dates],
        "indicators":{"quote":[{"open":[v-1 for v in values],
                                 "high":[v+1 for v in values],
                                 "low":[v-2 for v in values],
                                 "close":values,"volume":[100,200,300]}]}
    }]}}).encode()

def yahoo(url,**kwargs):
    ticker=url.rsplit("/",1)[-1]
    assert ticker in m.US
    return Response(us_payload(ticker),url)


def test_krx_night_cutoff_drops_todays_unclosed_intraday_bar():
    earlier=datetime(2026,10,21,11,50,tzinfo=timezone.utc) # 20:50 KST
    raw,rows=m.fetch_naver("005930",earlier,getter=naver)
    assert rows[-1][0]=="2026-10-20"
    done=datetime(2026,10,21,12,50,tzinfo=timezone.utc)
    raw,rows=m.fetch_naver("005930",done,getter=naver)
    assert rows[-1][0]=="2026-10-21"
    assert all(row[1]>0 for row in rows)

def test_us_closure_uses_real_new_york_timezone_and_daylight_saving():
    now=datetime(2026,10,21,13,0,tzinfo=timezone.utc) # 9AM NY
    assert m.provider_cutoff(now,"NASDAQ_NYSE")=="2026-10-20"
    _,rows=m.fetch_yahoo("AAPL",now,getter=yahoo)
    assert rows[-1][0]=="2026-10-20"
    later=datetime(2026,10,21,23,30,tzinfo=timezone.utc) # 19:30 NY
    _,rows=m.fetch_yahoo("AAPL",later,getter=yahoo)
    assert rows[-1][0]=="2026-10-21"
    winter=datetime(2026,12,21,22,50,tzinfo=timezone.utc)
    assert m.provider_cutoff(winter,"NASDAQ_NYSE")=="2026-12-20"
    assert m.provider_cutoff(winter+timedelta(hours=2),"NASDAQ_NYSE")=="2026-12-21"

def test_real_six_asset_sources_are_isolated_from_crypto_and_deduplicated(tmp_path):
    path=tmp_path/"stock_daily"/"observations.sqlite3"
    first=m.capture(path,now=NOW,naver_getter=naver,yahoo_getter=yahoo)
    assert first["status"]=="STOCK_PUBLIC_DAILY_DATA_CAPTURED_NO_TRADING"
    assert sum(x["new_completed_bars"] for x in first["results"])==15
    assert not first["real_orders_enabled"]
    with sqlite3.connect(path) as c:
        assert c.execute("select count(*) from daily_bars").fetchone()[0]==15
        assert c.execute("select count(*) from source_snapshots").fetchone()[0]==6
    second=m.capture(path,now=NOW,naver_getter=naver,yahoo_getter=yahoo)
    assert all(x["new_completed_bars"]==0 for x in second["results"])
    with sqlite3.connect(path) as c:
        assert c.execute("select count(*) from source_snapshots").fetchone()[0]==6
        assert c.execute("select count(*) from revision_alerts").fetchone()[0]==0
    audit=m.inspect(path)
    assert audit["last_12_raw_payload_sha256_verified"]
    assert not audit["auto_market_model_enabled"]
    assert audit["dividends_splits_total_return_not_verified"]

def test_historical_provider_revision_flagged_preserves_immutable_first_bar(tmp_path):
    path=tmp_path/"stock_daily"/"observations.sqlite3"
    m.capture(path,now=NOW,naver_getter=naver,yahoo_getter=yahoo)
    def changed(url,**kwargs):
        result=naver(url,**kwargs)
        result.content=result.content.replace(b"101, 123",b"103, 123")
        return result
    result=m.capture(path,now=NOW,naver_getter=changed,yahoo_getter=yahoo)
    assert result["status"]=="STOCK_DATA_DEGRADED_NO_TRADING"
    assert sum(x.get("provider_revisions",0) for x in result["results"])==3
    with sqlite3.connect(path) as c:
        old=c.execute("select close from daily_bars where ticker='005930' and session_date='2026-10-19'").fetchone()[0]
        assert old==101
        assert c.execute("select count(*) from revision_alerts").fetchone()[0]==3
        assert c.execute("select count(*) from source_snapshots").fetchone()[0]==9

def test_bad_provider_cannot_overwrite_prior_good_market_data(tmp_path):
    path=tmp_path/"stock_daily"/"observations.sqlite3"
    def bad_us(url,**kwargs):
        data=json.loads(us_payload(url.rsplit("/",1)[-1]))
        data["chart"]["result"][0]["meta"]["currency"]="INVALID"
        return Response(json.dumps(data).encode(),url)
    result=m.capture(path,now=NOW,naver_getter=naver,yahoo_getter=bad_us)
    assert result["status"]=="STOCK_DATA_DEGRADED_NO_TRADING"
    assert [x["status"] for x in result["results"]].count("PASS")==3
    assert [x["status"] for x in result["results"]].count("FETCH_OR_VALIDATION_FAILED")==3
    with sqlite3.connect(path) as c:
        assert c.execute("select count(*) from daily_bars").fetchone()[0]==9
        assert c.execute("select count(*) from fetch_failures").fetchone()[0]==3

def test_bad_redirects_invalid_values_and_nonpermitted_markets_rejected():
    with pytest.raises(ValueError,match="Unsupported"):
        m.fetch_naver("000001",NOW,getter=naver)
    with pytest.raises(ValueError,match="Unsupported"):
        m.fetch_yahoo("BTC-USD",NOW,getter=yahoo)
    with pytest.raises(ValueError,match="OHLC"):
        m.validate_bars([("2026-10-01",100,99,98,100,5)],maximum_day="2026-10-20")
    with pytest.raises(ValueError,match="Duplicate"):
        m.validate_bars([("2026-10-01",100,102,99,100,5)]*2,maximum_day="2026-10-20")
    with pytest.raises(ValueError,match="Unclosed|unclosed"):
        m.validate_bars([("2026-10-22",100,102,99,100,5)],maximum_day="2026-10-20")

def test_raw_payload_tamper_found_by_independent_audit(tmp_path):
    path=tmp_path/"stock_daily"/"observations.sqlite3"
    m.capture(path,now=NOW,naver_getter=naver,yahoo_getter=yahoo)
    with sqlite3.connect(path) as c:
        c.execute("UPDATE source_snapshots SET raw=? WHERE id=1",(b"TAMPERED",))
    assert not m.inspect(path)["last_12_raw_payload_sha256_verified"]

def test_rejects_sharing_crypto_or_qlib_trading_ledger(tmp_path):
    with pytest.raises(ValueError,match="isolated"):
        m.capture(tmp_path/"market_history.db",now=NOW,naver_getter=naver,yahoo_getter=yahoo)
    with pytest.raises(ValueError,match="isolated"):
        m.capture(tmp_path/"shadow.db",now=NOW,naver_getter=naver,yahoo_getter=yahoo)

def test_stock_source_stale_is_reported_and_never_promoted(tmp_path):
    path=tmp_path/"stock_daily"/"observations.sqlite3"
    future=datetime(2026,11,9,15,tzinfo=timezone.utc)
    result=m.capture(path,now=future,naver_getter=naver,yahoo_getter=yahoo)
    assert result["status"]=="STOCK_DATA_DEGRADED_NO_TRADING"
    assert all(x["status"]=="STALE_MARKET_VENDOR_NO_NEW_DAILY_DATA"
               for x in result["results"])
    assert result["actual_broker_orders"]==0


def test_systemd_stock_market_not_cryptocurrency_and_safe_startup():
    root=Path(__file__).resolve().parents[1]/"deploy"/"systemd"
    service=(root/"freequant-stock-observer.service").read_text()
    timer=(root/"freequant-stock-observer.timer").read_text()
    assert service.count("ExecStart=")==2
    assert "stock_daily_observer --capture" in service
    assert "stock_daily_observer --audit" in service
    assert "state/stock_daily/observations.sqlite3" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=read-only" in service
    assert "ReadWritePaths=/home/hong/apps/FreeQuantLab/state/stock_daily" in service
    assert "state/shadow.db" not in service
    assert "09:30:00" in timer and "21:30:00" in timer
    assert "OnBootSec=20min" in timer and "Persistent=true" in timer

"""No phantom ETF distributions, no imaginary zero-liquidity fills."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import pandas as pd
import pytest

from quantlab.continuous_portfolio import ASSETS, continuous_account
from quantlab.etf_issuer_evidence import (
    PRODUCTS,ROOT,fetch_official_evidence,integrate_evidence,
)
from quantlab.liquidity_audit import audit_trace


def market(n=800,volume=1000.):
    dates=pd.date_range("2020-01-01",periods=n)
    return {s:pd.DataFrame({
        "date":dates,
        "open":[float(100*(i+1))]*n,
        "close":[float(100*(i+1))]*n,
        "high":[float(100*(i+1)*1.01)]*n,
        "low":[float(100*(i+1)*.99)]*n,
        "volume":[volume]*n,
    }) for i,s in enumerate(ASSETS)}


def basic_trace(data,fee=15):
    return continuous_account(data,{s:[1.]*len(data[s]) for s in ASSETS},
                              600,650,fee_bps=fee)


def test_conservative_volume_screen_all_zero_rejections_when_liquid():
    df=market(volume=1_000_000.)
    report=audit_trace(df,basic_trace(df),participation=.0025)
    assert report["status"]=="BOUNDED_HISTORICAL_VOLUME_SCREEN_ONLY"
    assert report["orders_checked"]==8
    assert report["failed_orders"]==0
    assert report["estimated_additional_cost_krw_if_ALL_original_orders_filled"]>0
    assert report["new_net_roi_not_computed"] is True
    assert report["real_orders_enabled"] is False


def test_no_fill_when_ex_post_volume_limit_is_too_small():
    df=market(volume=0.001)
    trace=basic_trace(df)
    report=audit_trace(df,trace,participation=.01)
    assert report["status"]=="NOT_EXECUTABLE_AT_CAP"
    assert report["failed_orders"]>0
    assert report["original_equity_performance_valid_under_this_screen"] is False
    assert report["failure_examples"][0]["max_conservative_notional_krw"]>0
    assert not report["auto_promotion_allowed"]


def test_volume_caps_monotonic_on_same_hypothetical_trace():
    df=market(volume=30)
    trace=basic_trace(df)
    tiers=[audit_trace(df,trace,participation=x) for x in (.0025,.01,.05)]
    assert tiers[0]["failed_orders"]>=tiers[1]["failed_orders"]>=tiers[2]["failed_orders"]


def test_zero_volume_and_invalid_evidence_fail_closed():
    df=market()
    original=basic_trace(df)
    faulty=copy.deepcopy(original)
    faulty["hypothetical_fills"][0]["quantity"]*=2
    with pytest.raises(ValueError,match="notional"):
        audit_trace(df,faulty)
    faulty=copy.deepcopy(original)
    faulty["orders"]+=1
    with pytest.raises(ValueError,match="Invalid hypothetical order"):
        audit_trace(df,faulty)
    df["KRW-BTC"].loc[601:601,"volume"]=0
    report=audit_trace(df,original)
    assert report["failed_orders"]>=1
    with pytest.raises(ValueError):
        audit_trace(market(),original,participation=.5)


def test_same_day_multiple_trades_share_one_daily_volume_cap():
    df=market(volume=100)
    first=basic_trace(df)
    # Duplicate a valid buy and keep the fixed-fee ledger balanced.
    event=copy.deepcopy(first["hypothetical_fills"][0])
    trace=copy.deepcopy(first)
    trace["hypothetical_fills"].insert(1,event)
    trace["orders"]+=1
    trace["paid_fees_and_slippage_krw"]+=event["cost"]
    x=audit_trace(df,trace,participation=.0025)
    assert x["failed_orders"]>=1
    assert x["failure_examples"][0]["asset"]=="KRW-BTC"


class _FakeResponse:
    def __init__(self,code,name,url):
        self.url=url
        self.content=(f"<html><head><title>{name} ETF | Kodex</title></head>"
                      f"<body>{code} 분배금 지급현황</body></html>").encode()
    def raise_for_status(self):
        return None


def test_issuer_official_page_identity_not_historical_distribution_proof():
    def fake(url,**kwargs):
        product_id=url.rsplit("=",1)[1]
        for ticker,obj in PRODUCTS.items():
            if obj["product_id"]==product_id:
                return _FakeResponse(ticker,obj["official_name"],url)
        pytest.fail("Unexpected product URL")
    r=fetch_official_evidence(fetcher=fake)
    assert len(r["products"])==3
    assert not r["eligible_for_etf_total_return_backtest"]
    assert all(not p["full_historical_cash_distributions_verified"] for p in r["products"].values())


def test_issuer_wrong_ticker_fails_closed():
    def fake(url,**kwargs):
        return _FakeResponse("INVALID","KODEX 200",url)
    with pytest.raises(ValueError,match="mismatch"):
        fetch_official_evidence(fetcher=fake)


def test_issuer_redirect_host_not_accepted():
    def fake(url,**kwargs):
        return _FakeResponse("069500","KODEX 200","https://evil.example/product?id=2ETF01")
    with pytest.raises(ValueError,match="redirected"):
        fetch_official_evidence(fetcher=fake)


def test_issuer_metadata_cannot_override_etf_total_return_gate(monkeypatch):
    import quantlab.etf_issuer_evidence as module
    issuer=fetch_official_evidence(fetcher=lambda url,**kwargs:
        _FakeResponse(
            next(s for s,m in PRODUCTS.items() if url.endswith(m["product_id"])),
            next(m["official_name"] for s,m in PRODUCTS.items() if url.endswith(m["product_id"])),
            url))
    initial={"funds":{s:{} for s in PRODUCTS},
             "status":"BLOCKED_TOTAL_RETURN_VERIFICATION",
             "total_return_performance_publishable":False}
    monkeypatch.setattr(module,"audit_etf_snapshot",lambda path:copy.deepcopy(initial))
    result=integrate_evidence("unused",issuer)
    assert result["official_product_identity_verified"]
    assert not result["official_complete_distribution_events_verified"]
    assert not result["total_return_performance_publishable"]
    assert not result["automatic_strategy_promotion"]


def test_sized_ticket_stress_can_invalidate_old_backtest_without_claiming_new_pnl():
    from quantlab.liquidity_audit import proportionally_scaled_order_trace
    data=market(volume=1000.)
    base=basic_trace(data)
    scaled=proportionally_scaled_order_trace(base,1000)
    assert scaled["not_a_recomputed_capital_return"] is True
    assert scaled["capacity_only_hypothetical_initial_cash_krw"]==1_000_000_000.
    assert scaled["hypothetical_fills"][0]["notional"]==pytest.approx(
        base["hypothetical_fills"][0]["notional"]*1000)
    poor=audit_trace(data,scaled,participation=.0025)
    assert poor["failed_orders"]>0
    assert poor["new_net_roi_not_computed"] is True
    assert poor["original_equity_performance_valid_under_this_screen"] is False
    with pytest.raises(ValueError,match="Unregistered"):
        proportionally_scaled_order_trace(base,3)

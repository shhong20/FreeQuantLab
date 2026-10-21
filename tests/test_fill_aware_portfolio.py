"""Fill-aware cash/holdings conservation and pending-order lifecycle regression."""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from quantlab.continuous_portfolio import ASSETS
from quantlab.fill_aware_portfolio import fill_aware_account


def fixture(n=820,volume=1e6,*,growth=False):
    dates=pd.date_range("2020-01-01",periods=n)
    out={}
    for ix,asset in enumerate(ASSETS):
        opening=np.full(n,100.*(ix+1))
        if growth and asset=="KRW-BTC":
            opening[603:]*=2
        vols=np.full(n,float(volume))
        out[asset]=pd.DataFrame({
            "date":dates,"open":opening,"close":opening,
            "high":opening*1.01,"low":opening*.99,
            "volume":vols,
        })
    return out


def decisions(n=820, value=1.):
    return {asset:np.full(n,value) for asset in ASSETS}


def test_all_fills_once_if_unlimited_liquidity_constant_price():
    x=fill_aware_account(fixture(),decisions(),600,760,
                         fee_bps=0,spread_bps=0,impact_scale_bps=0)
    assert x["final_cash_krw"]==pytest.approx(1_000_000)
    assert x["total_marked_return"]==pytest.approx(0)
    assert x["residual_units_not_cash"]=={}
    assert x["counts"]["fills"]==8
    assert x["counts"]["created"]==8
    assert x["counts"]["partial_fills"]==0
    assert x["posttrade_risk_breach_count"]==0
    assert x["max_posttrade_gross_fraction"]<=.4+1e-8
    assert x["pending_order_count_at_end"]==0


def test_cash_and_adverse_fill_costs_exactly_reconcile():
    x=fill_aware_account(fixture(),decisions(),600,760)
    assert x["status"]=="RESEARCH_ONLY_NOT_EXECUTABLE_EVIDENCE"
    assert x["final_cash_krw"]<1_000_000
    assert x["fee_paid_krw"]>0
    assert x["adverse_price_proxy_cost_krw"]>0
    assert x["counts"]["fills"]>=8
    assert x["counts"]["created"]==8
    assert x["total_marked_return"]==pytest.approx(
        x["final_cash_krw"]/1_000_000-1)
    assert x["posttrade_risk_breach_count"]==0


def test_insufficient_historical_volume_has_pending_cancellations_no_phantom_nav():
    x=fill_aware_account(fixture(volume=0),decisions(),600,645)
    assert x["final_cash_krw"]==pytest.approx(1_000_000)
    assert x["total_marked_return"]==pytest.approx(0)
    assert not x["hypothetical_partial_fills"]
    assert x["counts"]["cancelled"]>0
    assert x["counts"]["expired"]>0
    assert x["unfilled_zero_capacity_attempts"]>0


def test_partial_fill_expires_and_unfilled_terminal_is_not_converted_to_cash():
    x=fill_aware_account(fixture(volume=2),decisions(),600,620,ttl_opens=3)
    assert x["counts"]["partial_fills"]>0
    assert x["counts"]["expired"]>0
    assert x["counts"]["cancelled"]>0
    assert x["residual_units_not_cash"]
    assert x["status"]=="RESIDUAL_NOT_LIQUIDATED"
    assert x["final_marked_equity_krw"]>x["final_cash_krw"]
    assert x["pending_order_count_at_end"]==0


def test_signal_flip_cancels_old_entry_and_requests_sell():
    signal=decisions()
    signal["KRW-BTC"][603:]=0
    x=fill_aware_account(fixture(volume=20),signal,600,620,ttl_opens=3)
    events=x["order_lifecycle"]
    assert any(e["status"]=="CANCELED" and e["asset"]=="KRW-BTC"
               for e in events)
    assert any(e["status"]=="CREATED" and e["asset"]=="KRW-BTC"
               and e["side"]=="SELL" for e in events)
    assert not any(fill["fill_units"]<0 for fill in x["hypothetical_partial_fills"])


def test_overnight_risk_breach_cannot_claim_fully_enforced_cap_if_volume_limited():
    x=fill_aware_account(fixture(volume=2,growth=True),decisions(),600,625)
    assert x["pretrade_gross_breach_count"]>=0
    assert x["max_posttrade_gross_fraction"]>=0
    assert x["risk_limit_cannot_be_guaranteed_during_zero_volume"]
    assert x["real_orders_enabled"] is False


def test_invalid_future_or_short_signal_and_asset_fail_closed():
    invalid=decisions()
    invalid["KRW-BTC"][600]=np.nan
    with pytest.raises(ValueError):
        fill_aware_account(fixture(),invalid,600,650)
    invalid=decisions()
    invalid["KRW-BTC"][600]=-1
    with pytest.raises(ValueError):
        fill_aware_account(fixture(),invalid,600,650)
    frame=fixture()
    frame["069500"]=frame.pop("KRW-XRP")
    with pytest.raises(ValueError):
        fill_aware_account(frame,decisions(),600,650)
    with pytest.raises(ValueError):
        fill_aware_account(fixture(),decisions(),600,650,participation=0)


def test_increasing_future_decisions_does_not_change_previous_equity():
    a=decisions()
    b=decisions()
    b["KRW-ADA"][720:]=0
    original=fill_aware_account(fixture(),a,600,770)
    altered=fill_aware_account(fixture(),b,600,770)
    assert original["equity_path"][:118]==altered["equity_path"][:118]
    assert original["order_lifecycle"]!=altered["order_lifecycle"]


def test_same_market_and_policies_replay_exactly():
    a=fill_aware_account(fixture(volume=100),decisions(),600,750)
    b=fill_aware_account(fixture(volume=100),decisions(),600,750)
    assert a==b


def test_event_cash_conservation_and_same_date_capacity_per_asset():
    from collections import defaultdict
    data=fixture(volume=25)
    result=fill_aware_account(data,decisions(),600,750,initial_cash=1_000_000,
                              participation=.01)
    reconstructed=1_000_000.
    used=defaultdict(float)
    caps={}
    for fill in result["hypothetical_partial_fills"]:
        key=(fill["date"],fill["asset"])
        used[key]+=fill["market_notional_krw"]
        caps[key]=fill["volume_cap_prior_20d_krw"]
        cash_delta=fill["cash_notional_krw"]
        fee=fill["fee_krw"]
        reconstructed+=(cash_delta-fee if fill["side"]=="SELL"
                         else -cash_delta-fee)
    assert reconstructed==pytest.approx(result["final_cash_krw"],abs=1e-6)
    assert all(used[k]<=caps[k]+1e-6 for k in caps)
    end_open_index=751
    marked=result["final_cash_krw"]+sum(
        quantity*float(data[asset]["open"].iloc[end_open_index])
        for asset,quantity in result["residual_units_not_cash"].items()
    )
    assert marked==pytest.approx(result["final_marked_equity_krw"],rel=1e-10)


def test_fill_open_cannot_use_that_days_unfinished_volume_as_capacity():
    data=fixture(volume=100)
    original=fill_aware_account(data,decisions(),600,620,
                                initial_cash=1e7,participation=.0025)
    changed=fixture(volume=100)
    # Candle 601 volume is unknown at the 601 opening; the cap must
    # be computed entirely from the preceding completed 20 candles.
    changed["KRW-BTC"].at[601,"volume"]=1e12
    altered=fill_aware_account(changed,decisions(),600,620,
                               initial_cash=1e7,participation=.0025)
    old_first=[f for f in original["hypothetical_partial_fills"]
               if f["open_index"]==601]
    new_first=[f for f in altered["hypothetical_partial_fills"]
               if f["open_index"]==601]
    assert old_first==new_first
    assert altered["synthetic_quote_no_actual_orderbook"]
    assert altered["daily_total_volume_not_assumed_available_at_open"]


def test_terminal_residual_is_never_silently_marked_as_liquidated():
    data=fixture(volume=1)
    x=fill_aware_account(data,decisions(),600,680,initial_cash=100_000_000,
                         participation=.0025)
    assert x["residual_units_not_cash"]
    assert x["final_marked_equity_krw"]>x["final_cash_krw"]
    assert x["status"]=="RESIDUAL_NOT_LIQUIDATED"
    assert x["eligible_for_real_trading"] is False
    assert x["pending_order_count_at_end"]==0

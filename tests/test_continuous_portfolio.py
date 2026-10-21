"""No synthetic backfill/portfolio funding/ETF gate regression tests."""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from quantlab.continuous_portfolio import (
    ASSETS, START, continuous_account, verify_aligned_crypto, policies,
    load_crypto_snapshots,
)
from quantlab.etf_price_audit import audit_etf_snapshot


def markets(n=820,*,growth=False):
    dates=pd.date_range("2020-01-01",periods=n)
    out={}
    for idx,s in enumerate(ASSETS):
        openp=np.full(n,100.*(idx+1))
        if growth and s=="KRW-BTC":
            openp[603:]*=2
        out[s]=pd.DataFrame({
            "date":dates,
            "open":openp,"high":openp*1.02,"low":openp*.98,
            "close":openp,"volume":np.ones(n)*100,
        })
    return out


def signals(n=820,value=1.):
    return {s:np.full(n,value) for s in ASSETS}


def test_constant_market_zero_cost_and_account_does_not_reset():
    data=markets()
    got=continuous_account(data,signals(),600,800,fee_bps=0)
    assert got["final_cash_krw"]==pytest.approx(1_000_000)
    assert got["total_return"]==pytest.approx(0)
    assert got["orders"]==8  # 4 buys, 4 final exits, no fold resets
    assert all(t["reason"]!="risk_exposure_trim" for t in got["hypothetical_fills"])
    assert got["risk_exposure_trim_orders"]==0
    assert got["equity_path"][119]["equity_krw"]==pytest.approx(1_000_000)
    assert len(got["equity_path"])==200
    assert got["max_gross_fraction_at_fill"]<=.4+1e-9


def test_actual_fee_notional_cash_conservation():
    got=continuous_account(markets(),signals(),600,800,fee_bps=15)
    assert got["orders"]==8
    assert got["risk_exposure_trim_orders"]==0
    assert got["paid_fees_and_slippage_krw"]>0
    assert got["final_cash_krw"]==pytest.approx(
        1_000_000-got["paid_fees_and_slippage_krw"],rel=1e-10
    )
    assert got["final_cash_krw"]<1_000_000
    assert got["final_invested_krw"]==0


def test_gainers_trim_to_portfolio_cap_without_new_cash_or_retrain_liquidation():
    got=continuous_account(markets(growth=True),signals(),600,800,fee_bps=15)
    assert got["max_gross_fraction_before_next_open_orders"]>.4
    assert got["max_gross_fraction_at_fill"]<=.4+1e-9
    assert got["risk_exposure_trim_orders"]>0
    assert got["total_return"]>0
    assert got["final_cash_krw"]>1_000_000
    assert all(t["side"] in ("BUY","SELL") for t in got["hypothetical_fills"])
    assert all(t["notional"]>0 and t["quantity"]>0 for t in got["hypothetical_fills"])


def test_signal_transition_pays_costs_and_avoids_forced_fold_boundary_exits():
    sig=signals()
    for s in ASSETS: sig[s][650:]=0
    sig["KRW-BTC"][700:]=1
    result=continuous_account(markets(),sig,600,800,fee_bps=15)
    assert result["orders"]==4+4+1+1
    sell=[x for x in result["hypothetical_fills"] if x["side"]=="SELL"]
    assert not any(x["date"]==str(markets()["KRW-BTC"].date.iloc[721].date())
                   for x in sell)
    assert result["risk_exposure_trim_orders"]==0


def test_crypto_rejects_missing_day_misalignment_or_equity_market():
    data=markets()
    data["KRW-XRP"]=data["KRW-XRP"].drop(index=150).reset_index(drop=True)
    with pytest.raises(ValueError):
        verify_aligned_crypto(data)
    data=markets()
    data["069500"]=data.pop("KRW-ADA")
    with pytest.raises(ValueError,match="crypto"):
        continuous_account(data,signals(),600,800)
    bad=signals()
    bad["KRW-BTC"][600]=np.nan
    with pytest.raises(ValueError,match="Missing"):
        continuous_account(markets(),bad,600,800)
    bad=signals()
    bad["KRW-BTC"][600]=-1
    with pytest.raises(ValueError,match="short"):
        continuous_account(markets(),bad,600,800)


def test_etf_snapshot_full_sha_audit_blocks_total_return(tmp_path):
    market="krx_local_sessions"
    source_root=tmp_path/"snapshot"
    source_root.mkdir()
    dates=pd.date_range("2020-01-01",periods=820,freq="B")
    obj={"groups":{market:{"source":"069500","target_ids":["114800","132030"],"assets":{}}}}
    for s in ("069500","114800","132030"):
        rows=pd.DataFrame({
            "date":dates,"open":100.,"high":101.,"low":99.,
            "close":100.,"volume":50.,
        })
        name=f"{market}_{s}.csv"
        content=rows.to_csv(index=False).encode()
        (source_root/name).write_bytes(content)
        from hashlib import sha256
        obj["groups"][market]["assets"][s]={
            "filename":name,"sha256":sha256(content).hexdigest(),
            "records":len(rows),"first":str(dates[0].date()),
            "last":str(dates[-1].date()),
        }
    (source_root/"manifest.json").write_text(json.dumps(obj))
    audit=audit_etf_snapshot(source_root)
    assert audit["status"]=="BLOCKED_TOTAL_RETURN_VERIFICATION"
    assert audit["shared_trading_sessions"]==820
    assert not audit["total_return_performance_publishable"]
    assert all(not r["fund_total_return_ready"] for r in audit["funds"].values())
    (source_root/f"{market}_114800.csv").write_text("tampered")
    with pytest.raises(ValueError,match="SHA256"):
        audit_etf_snapshot(source_root)


def test_real_crypto_archive_hashed_and_fully_aligned():
    src=Path("/home/hong/apps/FreeQuantLab-crossmarket-20261021/reports/cross-market-snapshot-20261021")
    hist=Path("/home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021")
    if not src.is_dir() or not hist.is_dir():
        pytest.skip("Only on local WSL where original frozen CSVs are mounted")
    data,proof=load_crypto_snapshots(src,hist)
    assert len(verify_aligned_crypto(data)["KRW-BTC"])==3000
    assert len({proof[s]["sha256"] for s in ASSETS})==4


def test_risk_capped_synthetic_fee_stress_and_no_discretionary_rebalances():
    data=markets(growth=True)
    got=[continuous_account(data,signals(),600,800,fee_bps=x) for x in (15,30,60)]
    assert got[0]["total_return"]>=got[1]["total_return"]>=got[2]["total_return"]
    assert all(x["max_gross_fraction_at_fill"]<=.4+1e-9 for x in got)
    assert all(x["max_gross_fraction_before_next_open_orders"]>.4 for x in got)
    assert got[0]["risk_exposure_trim_orders"]>0
    assert not any(x["reason"]=="fold_boundary_reset"
                   for x in got[0]["hypothetical_fills"])


def test_months_and_future_signals_cannot_retroactively_change_prior_equity():
    df=markets()
    first=signals()
    second=signals()
    second["KRW-ETH"][720:]=0
    a=continuous_account(df,first,600,800,fee_bps=15)
    b=continuous_account(df,second,600,800,fee_bps=15)
    # A changed decision at day 720 executes at the following open, and
    # cannot alter recorded capital levels before that opening.
    assert a["equity_path"][:119]==b["equity_path"][:119]
    assert a["hypothetical_fills"]!=b["hypothetical_fills"]
    assert any(x["asset"]=="KRW-ETH" and x["reason"]=="signal_off"
               for x in b["hypothetical_fills"])
    assert not any(x["asset"]=="KRW-ETH" and x["reason"]=="signal_off"
                   for x in a["hypothetical_fills"])


def test_real_qlib_signal_retrain_stitch_smoke_has_no_test_label_leak(tmp_path):
    pytest.importorskip("qlib")
    import quantlab.continuous_portfolio as mod
    from quantlab.data import demo_daily
    data={}
    for j,s in enumerate(ASSETS):
        frame=demo_daily(820)
        frame["date"]=pd.date_range("2020-01-01",periods=820)
        for col in ("open","high","low","close"):
            frame[col]*=(j+1)
        data[s]=frame
    # 4 training calls * 2 folds; no target/exposure writes to shared ledgers.
    stitched,bounds=mod.qlib_causal_signals(data,state_dir=tmp_path/"q")
    assert len(bounds)==4*len(range(600,818,120))
    assert all(b["training_known_before"]<=b["oos_end_exclusive"] for b in bounds)
    assert all(np.isfinite(stitched[s][600:818]).all() for s in ASSETS)
    assert all(np.isnan(stitched[s][:600]).all() for s in ASSETS)
    result=continuous_account(data,stitched,600,818,fee_bps=15)
    assert result["mode"]=="ONE_CONTINUOUS_ACCOUNT_NO_FOLD_RESETS"
    assert not result["no_real_orders"] is False

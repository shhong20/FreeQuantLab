"""Continuous single-ledger, next-open, self-financing crypto research portfolio.

Unlike the earlier isolated-fold backtests, this does NOT reset capital,
liquidate at each Qlib retrain boundary, or synthesize retrospective trades.
One shared cash account holds fractional crypto units. All fills are hypothetical
next-open fills, without an executable orderbook, market impact, taxes, or
funding. ETF/stock input is deliberately rejected until distributions and
corporate actions are independently verified.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .daily_receipt import _safe_report
from .data import normalize
from .qlib_cross_market import validate_market, source_fit_target_predict
from .qlib_bridge import qlib_probabilities
from .settings import Settings

ASSETS=("KRW-BTC","KRW-ETH","KRW-XRP","KRW-ADA")
START=600
RETRAIN_EVERY=120
COST_BPS=(15.,30.,60.)
ASSET_TARGET_FRACTION=.10
MAX_PORTFOLIO_ENTRY_FRACTION=.40
CAPITAL_KRW=1_000_000.


def verify_aligned_crypto(frames:dict[str,pd.DataFrame])->dict[str,pd.DataFrame]:
    if set(frames)!=set(ASSETS):
        raise ValueError("Refuse missing or unknown crypto assets; ETF/equity prohibited")
    checks={}
    for symbol in ASSETS:
        if not symbol.startswith("KRW-"):
            raise ValueError("Only matched KRW spot crypto permitted")
        checks[symbol]=validate_market(frames[symbol],"upbit")
    dates=checks[ASSETS[0]]["date"]
    if any(not checks[s]["date"].equals(dates) for s in ASSETS[1:]):
        raise ValueError("Cross-asset UTC calendar mismatch; no forward-filling")
    return checks


def continuous_account(
    frames:dict[str,pd.DataFrame],
    signals:dict[str,np.ndarray],
    start:int,end:int,
    *,fee_bps:float=15.,initial_cash:float=CAPITAL_KRW,
    per_asset_target:float=ASSET_TARGET_FRACTION,
    max_new_gross_allocation:float=MAX_PORTFOLIO_ENTRY_FRACTION,
)->dict:
    data=verify_aligned_crypto(frames)
    n=len(data[ASSETS[0]])
    if (not 0<=start<end<=n-2 or set(signals)!=set(ASSETS)
        or not np.isfinite(fee_bps) or fee_bps<0 or fee_bps>1000
        or not np.isfinite(initial_cash) or initial_cash<=0
        or not 0<per_asset_target<=.2
        or not 0<max_new_gross_allocation<=1
        or per_asset_target*len(ASSETS)>max_new_gross_allocation+1e-12):
        raise ValueError("Invalid continuous allocation or OOS schedule")
    for asset in ASSETS:
        s=np.asarray(signals[asset])
        if len(s)!=n or not np.isfinite(s[start:end]).all() or not np.isin(s[start:end],(0.,1.)).all():
            raise ValueError("Missing, future, leveraged, or short signals")
    cash=float(initial_cash)
    holdings={s:0. for s in ASSETS}
    cost=fee_bps/10000.
    prices={s:data[s]["open"].to_numpy(dtype=float) for s in ASSETS}
    trade_events=[]
    equity=[initial_cash]
    snapshots=[]
    cumulative_fees=0.
    max_gross=0.
    active_observations=0
    max_pretrade_gross=0.
    risk_trim_orders=0

    def at_open(ix:int)->float:
        return cash+sum(holdings[s]*prices[s][ix] for s in ASSETS)

    for i in range(start,end):
        fill_index=i+1
        opens={s:float(prices[s][fill_index]) for s in ASSETS}
        nav_before=at_open(fill_index)
        if nav_before<=0 or not np.isfinite(nav_before):
            raise ValueError("Insolvent or invalid equity")
        max_pretrade_gross=max(max_pretrade_gross,
            sum(holdings[s]*opens[s] for s in ASSETS)/nav_before)
        # Exit signals first; sales genuinely fund any new opening positions.
        for s in ASSETS:
            if holdings[s]>0 and signals[s][i]==0:
                qty=holdings[s]
                notion=qty*opens[s]
                fee=notion*cost
                cash+=notion-fee
                holdings[s]=0.
                cumulative_fees+=fee
                trade_events.append({"date":data[s]["date"].iloc[fill_index].date().isoformat(),
                                     "asset":s,"side":"SELL","reason":"signal_off",
                                     "quantity":qty,"notional":notion,"cost":fee})
        # A held position keeps actual units and drifts naturally; no hidden
        # constant 20% rebalancing or per-fold forced liquidation.
        for s in ASSETS:
            if holdings[s] > 0 or signals[s][i]==0:
                continue
            value=at_open(fill_index)
            gross=sum(holdings[a]*opens[a] for a in ASSETS)
            budget=min(
                per_asset_target*value/(1.+cost),
                max(0.,max_new_gross_allocation*value-gross)/(1.+cost),
                cash/(1.+cost),
            )
            if budget<=1e-10:
                continue
            # Budget is trade notional. Cash pays fee separately.
            amount=budget/opens[s]
            fee=budget*cost
            cash-=budget+fee
            if cash < -1e-7:
                raise ValueError("Margin/leverage forbidden")
            cash=max(0.,cash)
            holdings[s]+=amount
            cumulative_fees+=fee
            trade_events.append({"date":data[s]["date"].iloc[fill_index].date().isoformat(),
                                 "asset":s,"side":"BUY","reason":"signal_on",
                                 "quantity":amount,"notional":budget,"cost":fee})
        # An initial allocation limit alone is not a permanent risk cap:
        # cryptocurrency winners drift toward >90% exposure if left alone.
        # All candidates obey the same *post-fill* exposure caps; adverse
        # overnight gaps can still break the cap before the next open.
        def trim_notional(asset:str, amount:float):
            nonlocal cash,cumulative_fees,risk_trim_orders
            if amount<=1e-8:
                return
            notion=min(amount,holdings[asset]*opens[asset])
            if notion<=1e-8:
                return
            qty=notion/opens[asset]
            fee=notion*cost
            holdings[asset]-=qty
            cash+=notion-fee
            cumulative_fees+=fee
            risk_trim_orders+=1
            trade_events.append({
                "date":data[asset]["date"].iloc[fill_index].date().isoformat(),
                "asset":asset,"side":"SELL","reason":"risk_exposure_trim",
                "quantity":qty,"notional":notion,"cost":fee,
            })
        for asset in ASSETS:
            nav=at_open(fill_index)
            notion=holdings[asset]*opens[asset]
            # Cut to 20% of post-fee NAV; ensure fees cannot undo the cap.
            excessive=(notion-.20*nav)/(1.-.20*cost)
            if excessive>1e-8:
                trim_notional(asset,excessive)
        nav=at_open(fill_index)
        gross=sum(holdings[asset]*opens[asset] for asset in ASSETS)
        excessive=(gross-max_new_gross_allocation*nav)/(
            1.-max_new_gross_allocation*cost)
        if excessive>1e-8:
            # Pro rata keeps the composition from becoming an undisclosed
            # discretionary asset selection policy.
            ratio=min(1.,excessive/gross)
            reductions={asset:holdings[asset]*opens[asset]*ratio for asset in ASSETS}
            for asset,amount in reductions.items():
                trim_notional(asset,amount)
        if any(q < -1e-9 or not np.isfinite(q) for q in holdings.values()):
            raise ValueError("Invalid hypothetical units")
        holdings={asset:max(0.,qty) for asset,qty in holdings.items()}
        nav_at_fill=at_open(fill_index)
        gross_at_fill=sum(holdings[s]*opens[s] for s in ASSETS)/nav_at_fill
        max_gross=max(max_gross,gross_at_fill)
        if gross_at_fill>max_new_gross_allocation+1e-9 or any(
            holdings[s]*opens[s]/nav_at_fill>.20+1e-9 for s in ASSETS
        ):
            raise ValueError("Post-fill gross risk cap failed")
        if nav_at_fill<=0:
            raise ValueError("Nonpositive NAV after fills")
        active_observations+=sum(q>0 for q in holdings.values())
        # One day of *unchanged actual units* between consecutive opens.
        next_ix=i+2
        nav_next=at_open(next_ix)
        if i==end-1:
            # Exit at open[end+1] only once at the very end of the entire OOS
            # horizon, NOT at Qlib retrain/fold boundaries.
            for s in ASSETS:
                if holdings[s]<=0:
                    continue
                q=holdings[s]
                notion=q*float(prices[s][next_ix])
                fee=notion*cost
                cash+=notion-fee
                holdings[s]=0.
                cumulative_fees+=fee
                trade_events.append({"date":data[s]["date"].iloc[next_ix].date().isoformat(),
                                     "asset":s,"side":"SELL","reason":"final_liquidation",
                                     "quantity":q,"notional":notion,"cost":fee})
            nav_next=at_open(next_ix)
        if nav_next<=0 or not np.isfinite(nav_next):
            raise ValueError("Nonpositive or invalid account equity")
        equity.append(float(nav_next))
        snapshots.append({"date":data[ASSETS[0]]["date"].iloc[next_ix].date().isoformat(),
                          "equity_krw":float(nav_next),
                          "cash_krw":float(cash),
                          "invested_krw":float(nav_next-cash)})
    if any(q!=0 for q in holdings.values()):
        raise ValueError("Final liquidation failed")
    e=np.asarray(equity,dtype=float)
    curve_returns=e[1:]/e[:-1]-1.
    peak=np.maximum.accumulate(e)
    dd=e/peak-1.
    annual_periods=365.
    std=float(curve_returns.std(ddof=1)) if len(curve_returns)>1 else 0.
    return {
        "mode":"ONE_CONTINUOUS_ACCOUNT_NO_FOLD_RESETS",
        "start_initial_cash_krw":float(initial_cash),
        "start_decision":data[ASSETS[0]]["date"].iloc[start].date().isoformat(),
        "end_decision":data[ASSETS[0]]["date"].iloc[end-1].date().isoformat(),
        "final_mark_date":data[ASSETS[0]]["date"].iloc[end+1].date().isoformat(),
        "days":end-start,
        "final_cash_krw":float(cash),
        "final_invested_krw":0.,
        "total_return":float(e[-1]/e[0]-1),
        "annualized_return":float((e[-1]/e[0])**(annual_periods/(end-start))-1),
        "max_drawdown":float(dd.min()),
        "sharpe":float(curve_returns.mean()/std*np.sqrt(annual_periods)) if std>1e-12 else 0.,
        "worst_daily_return":float(curve_returns.min()),
        "average_daily_return":float(curve_returns.mean()),
        "paid_fees_and_slippage_krw":float(cumulative_fees),
        "orders":len(trade_events),
        "buy_orders":sum(t["side"]=="BUY" for t in trade_events),
        "sell_orders":sum(t["side"]=="SELL" for t in trade_events),
        "max_gross_fraction_at_fill":float(max_gross),
        "max_gross_fraction_before_next_open_orders":float(max_pretrade_gross),
        "risk_exposure_trim_orders":risk_trim_orders,
        "post_fill_risk_cap_only_not_intraday_guarantee":True,
        "active_asset_days":int(active_observations),
        "equity_path":snapshots,
        "hypothetical_fills":trade_events,
        "assumed_cost_bps_per_side":float(fee_bps),
        "no_tax_spread_orderbook_liquidity":True,
        "no_margin_or_short":True,
        "no_real_orders":True,
    }


def policies(frames:dict[str,pd.DataFrame])->dict[str,dict[str,np.ndarray]]:
    data=verify_aligned_crypto(frames)
    result={"equal_weight_buy_hold":{}, "ma20_80_continuous":{}}
    for symbol,frame in data.items():
        close=frame["close"].astype(float)
        result["equal_weight_buy_hold"][symbol]=np.ones(len(frame),dtype=float)
        result["ma20_80_continuous"][symbol]=(
            close.rolling(20).mean()>close.rolling(80).mean()
        ).astype(float).to_numpy()
    return result


def qlib_causal_signals(frames:dict[str,pd.DataFrame],*,state_dir:str|Path)->tuple[dict[str,np.ndarray],list[dict]]:
    """Actual models retrained every 120 days; OOS decisions stitched, never reset cash.

    BTC/ETH train on their own historical data; XRP/ADA use BTC's source
    mature labels only, preserving the previously fixed cross-asset scenario.
    """
    data=verify_aligned_crypto(frames)
    n=len(data[ASSETS[0]])
    result={s:np.full(n,np.nan) for s in ASSETS}
    audits=[]
    for start in range(START,n-2,RETRAIN_EVERY):
        end=min(n-2,start+RETRAIN_EVERY)
        for s in ASSETS:
            if s in ("KRW-BTC","KRW-ETH"):
                pred=qlib_probabilities(data[s],start,state_dir)
                proof={"source":s,"target":s,"training_known_before":start}
            else:
                pred,evidence=source_fit_target_predict(data["KRW-BTC"],data[s],start,state_dir)
                if evidence["target_labels_used_in_fit"] or evidence["trained_source_max_label_open_index"]>=start:
                    raise ValueError("Qlib source-only chronology violated")
                proof={"source":"KRW-BTC","target":s,"training_known_before":start,
                       "source_maturity":evidence}
            p=np.asarray(pred,dtype=float)
            if len(p)!=n or not np.isfinite(p[start:end]).all() or ((p[start:end]<0)|(p[start:end]>1)).any():
                raise ValueError("Invalid Qlib OOS forecast")
            result[s][start:end]=(p[start:end]>=.55).astype(float)
            audits.append({"target":s,"fit_end_exclusive":start,
                           "oos_end_exclusive":end,**proof})
    return result,audits


def load_crypto_snapshots(cross_snapshot:str|Path,historical_snapshot:str|Path)->tuple[dict[str,pd.DataFrame],dict]:
    cross_root=Path(cross_snapshot)
    history_root=Path(historical_snapshot)
    cross=json.loads((cross_root/"manifest.json").read_text())
    history=json.loads((history_root/"results.json").read_text())
    datasets={}
    provenance={}
    for sym in ASSETS:
        if sym=="KRW-ETH":
            meta=history["datasets"][sym]
            file=history_root/meta["filename"]
        else:
            meta=cross["groups"]["crypto_utc_24h"]["assets"][sym]
            file=cross_root/meta["filename"]
        if file.name not in (sym.replace("-","_")+".csv",
                             "crypto_utc_24h_"+sym.replace("-","_")+".csv"):
            raise ValueError("Unknown snapshot file")
        raw=file.read_bytes()
        if sha256(raw).hexdigest()!=meta["sha256"]:
            raise ValueError("Market snapshot SHA-256 mismatch")
        df=pd.read_csv(file)
        datasets[sym]=df
        provenance[sym]={"file":str(file),"sha256":meta["sha256"],"rows":len(df)}
    verify_aligned_crypto(datasets)
    return datasets,provenance


def evaluate_snapshots(*,cross_snapshot:str|Path,historical_snapshot:str|Path,
                       output_dir:str|Path,state_dir:str|Path,
                       include_qlib:bool=True)->dict:
    root=Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Append-only report: cannot overwrite previous evaluation")
    data,provenance=load_crypto_snapshots(cross_snapshot,historical_snapshot)
    strategies=policies(data)
    proof=[]
    if include_qlib:
        qlib,proof=qlib_causal_signals(data,state_dir=state_dir)
        strategies["qlib_causal_stitched"]=qlib
    final={}
    for name,signals in strategies.items():
        final[name]={}
        for cost in COST_BPS:
            final[name][str(int(cost))]=continuous_account(
                data,signals,START,len(data[ASSETS[0]])-2,fee_bps=cost,
            )
    report={
        "status":"EXPLORATORY_CONTINUOUS_CRYPTO_PORTFOLIO_ONLY",
        "created_at_utc":datetime.now(timezone.utc).isoformat(),
        "market_data":provenance,
        "assets":list(ASSETS),
        "initial_shared_cash_krw":CAPITAL_KRW,
        "allocation_per_new_asset_fraction":ASSET_TARGET_FRACTION,
        "max_initial_portfolio_gross_fraction":MAX_PORTFOLIO_ENTRY_FRACTION,
        "fixed_policy_names":list(strategies),
        "strategy_results":final,
        "qlib_retraining_evidence":proof,
        "equities_or_ETFs_allowed":False,
        "ETF_total_return_audit_separate_and_blocked":True,
        "real_orders_enabled":False,
        "automatic_strategy_promotion":False,
        "no_fresh_holdout_claim":True,
        "no_executable_open_price_guarantee":True,
        "crypto_quote_gap_or_trading_halt_not_simulated":True,
        "notional_and_fractional_crypto_only":True,
    }
    root.mkdir(parents=True,exist_ok=True)
    _safe_report(root/"results.json",report)
    return report


def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser(description="Continuous shared-cash crypto portfolio research")
    ap.add_argument("--cross-snapshot",default="/home/hong/apps/FreeQuantLab-crossmarket-20261021/reports/cross-market-snapshot-20261021")
    ap.add_argument("--historical-snapshot",default="/home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021")
    ap.add_argument("--output-dir",default="reports/continuous-crypto-20261021")
    ap.add_argument("--state-dir",default="state/continuous_crypto_qlib")
    ap.add_argument("--without-qlib",action="store_true")
    args=ap.parse_args(argv)
    result=evaluate_snapshots(cross_snapshot=args.cross_snapshot,
                              historical_snapshot=args.historical_snapshot,
                              output_dir=args.output_dir,state_dir=args.state_dir,
                              include_qlib=not args.without_qlib)
    print(json.dumps({
        "status":result["status"],
        "report":str(Path(args.output_dir)/"results.json"),
        "strategies":{
            name:{k:{field:v[field] for field in (
                "total_return","max_drawdown","sharpe","orders",
                "paid_fees_and_slippage_krw","max_gross_fraction_at_fill",
                "final_cash_krw",
            )} for k,v in costs.items()}
            for name,costs in result["strategy_results"].items()
        },
        "etf_research_blocked":True,"real_orders_enabled":False,
    },ensure_ascii=False))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

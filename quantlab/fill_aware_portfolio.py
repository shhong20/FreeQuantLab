"""Daily-OHLCV *proxy* partial-fill portfolio with one continuous cash account.

All order decisions use completed close[t] signals. At open[t+1] a pending
order can fill PARTIALLY up to a fraction of the preceding 20 *completed*
sessions' KRW notionals (close*base volume). Unfilled units remain pending,
can be cancelled on signal reversal, and expire after fixed open attempts.
No future full-day volume is used to decide fills. Prices are an *explicit*
adverse-open proxy, NOT actual best bid/ask or proven executable quote depth.
Failed risk trims leave measured breaches; failed final sells leave actual
residual marked holdings, NEVER phantom cash or silently filled positions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import sqrt
from pathlib import Path
import json

import numpy as np
import pandas as pd

from .continuous_portfolio import (
    ASSETS, START, CAPITAL_KRW, ASSET_TARGET_FRACTION,
    MAX_PORTFOLIO_ENTRY_FRACTION, policies, qlib_causal_signals,
    load_crypto_snapshots, verify_aligned_crypto,
)
from .daily_receipt import _safe_report

LOOKBACK=20
TTL_OPENS=3
DEFAULT_PARTICIPATION=.01
DEFAULT_FEE_BPS=10.
DEFAULT_SPREAD_BPS=5.
DEFAULT_IMPACT_SCALE_BPS=40.
MAX_HOLDING=.20
TRIM_TARGET_GROSS=.395
TRIM_TARGET_NAME=.198
ENTRY_REPLENISH_GAP_FRACTION=.005  # avoid daily tiny top-up trades
FINAL_STATUS="RESEARCH_PROXY_NOT_EXECUTABLE_ORDERBOOK"


@dataclass
class PendingOrder:
    order_id:int
    asset:str
    side:str
    reason:str
    remaining_units:float
    created_decision:int
    opened_at:int
    attempts:int=0


def fill_aware_account(
    frames:dict[str,pd.DataFrame],
    signals:dict[str,np.ndarray],
    start:int,end:int, *,
    initial_cash:float=CAPITAL_KRW,
    participation:float=DEFAULT_PARTICIPATION,
    fee_bps:float=DEFAULT_FEE_BPS,
    spread_bps:float=DEFAULT_SPREAD_BPS,
    impact_scale_bps:float=DEFAULT_IMPACT_SCALE_BPS,
    ttl_opens:int=TTL_OPENS,
)->dict:
    data=verify_aligned_crypto(frames)
    n=len(data[ASSETS[0]])
    if (not LOOKBACK<start<end<=n-2 or set(signals)!=set(ASSETS)
        or not np.isfinite(initial_cash) or initial_cash<=0
        or not np.isfinite(participation) or not 0<participation<=.1
        or type(ttl_opens)!=int or not 1<=ttl_opens<=10
        or any(not np.isfinite(x) or not 0<=x<=1000
               for x in (fee_bps,spread_bps,impact_scale_bps))):
        raise ValueError("Invalid causal execution model or assets")
    for s in ASSETS:
        a=np.asarray(signals[s],dtype=float)
        if len(a)!=n or not np.isfinite(a[start:end]).all() or not np.isin(a[start:end],(0.,1.)).all():
            raise ValueError("Invalid causal binary decision stream")

    opens={s:data[s]["open"].to_numpy(dtype=float) for s in ASSETS}
    close={s:data[s]["close"].to_numpy(dtype=float) for s in ASSETS}
    notionals={}
    for s in ASSETS:
        daily=close[s]*data[s]["volume"].to_numpy(dtype=float)
        if not np.isfinite(daily).all() or (daily<0).any():
            raise ValueError("Unknown daily base volume or close notional")
        notionals[s]=daily
    positions={s:0. for s in ASSETS}
    pending:dict[str,PendingOrder]={}
    initial=float(initial_cash)
    cash=initial
    next_id=0
    fee_paid=0.
    estimated_price_impact_cost=0.
    fill_events=[]
    order_events=[]
    equity=[initial]
    daily_path=[]
    counts=Counter()
    max_post_trade_gross=0.
    pretrade_breaches=0
    posttrade_breaches=0
    unfilled_cap_attempts=0

    def nav(index:int)->float:
        return cash+sum(positions[s]*opens[s][index] for s in ASSETS)

    def gross(index:int)->float:
        account=nav(index)
        return sum(positions[s]*opens[s][index] for s in ASSETS)/account

    def new_order(s:str,side:str,reason:str,units:float,decision:int,at:int):
        nonlocal next_id
        if s in pending or units<=1e-12:
            return
        next_id+=1
        pending[s]=PendingOrder(next_id,s,side,reason,units,decision,at)
        order_events.append({"status":"CREATED","order_id":next_id,"asset":s,
                             "side":side,"reason":reason,"decision_index":decision,
                             "first_eligible_open_index":at,"requested_units":units})
        counts["created"]+=1

    def cancel(s:str,reason:str,at:int):
        order=pending.pop(s,None)
        if order is None:
            return
        order_events.append({"status":"CANCELED","order_id":order.order_id,
                             "asset":s,"at_open_index":at,
                             "reason":reason,"remaining_units":order.remaining_units})
        counts["cancelled"]+=1

    def process_open(index:int,decision:int,*,terminal=False):
        nonlocal cash,fee_paid,estimated_price_impact_cost
        nonlocal max_post_trade_gross,pretrade_breaches,posttrade_breaches,unfilled_cap_attempts
        before=nav(index)
        if not np.isfinite(before) or before<=0:
            raise ValueError("Non-positive account before fill")
        if gross(index)>MAX_PORTFOLIO_ENTRY_FRACTION+1e-9:
            pretrade_breaches+=1
        # Volume at index is NOT known at the previous decision close.
        # The median of [index-20,index) is already settled before this open.
        budgets={}
        median={}
        attempted_assets=set()
        for s in ASSETS:
            history=notionals[s][index-LOOKBACK:index]
            median[s]=float(np.median(history))
            budgets[s]=max(0.,participation*median[s])
        for s in ASSETS:
            desire=0 if terminal else int(signals[s][decision])
            p=pending.get(s)
            if p and (
                (p.side=="BUY" and desire==0)
                or (p.reason=="signal_off" and desire==1)
                or (p.reason=="risk_trim" and desire==0)
                or (p.side=="BUY" and (
                    gross(index)>MAX_PORTFOLIO_ENTRY_FRACTION or
                    positions[s]*opens[s][index]/nav(index)>MAX_HOLDING
                ))
            ):
                cancel(s,"superseded_by_new_close_signal",index)
        # Exit requests precede all entries. Cancel stale risk sells if now safe.
        for s in ASSETS:
            desire=0 if terminal else int(signals[s][decision])
            p=pending.get(s)
            if p and p.reason=="risk_trim" and (positions[s]*opens[s][index]<=TRIM_TARGET_NAME*nav(index)+1e-7 and gross(index)<=TRIM_TARGET_GROSS+1e-7):
                cancel(s,"risk_no_longer_exceeded",index)
            if desire==0 and positions[s]>1e-12 and s not in pending:
                new_order(s,"SELL","terminal" if terminal else "signal_off",
                          positions[s],decision,index)
        # Per-name/gross cap trims are queued but cannot bypass volume limits.
        if not terminal:
            account=nav(index)
            total_notional=sum(positions[k]*opens[k][index] for k in ASSETS)
            global_reduction=(max(0.,total_notional-TRIM_TARGET_GROSS*account)
                              if total_notional>MAX_PORTFOLIO_ENTRY_FRACTION*account
                              else 0.)
            # Trim proportionally among ACTUALLY held units; never divide
            # by all four symbols when only one or two are held.
            safety=1.01
            for s in ASSETS:
                if s in pending or positions[s]<=1e-12:
                    continue
                market_value=positions[s]*opens[s][index]
                over_name=(max(0.,market_value-TRIM_TARGET_NAME*account)
                           if market_value>MAX_HOLDING*account else 0.)
                proportional=(global_reduction*market_value/total_notional
                              if total_notional>0 else 0.)
                reduce_value=max(over_name,proportional)*safety
                if reduce_value>1e-7:
                    new_order(s,"SELL","risk_trim",
                              min(positions[s],reduce_value/opens[s][index]),
                              decision,index)

        # Priority: selling exposure before any buys. A rejected or partly
        # filled sell NEVER credits phantom proceeds or releases phantom risk.
        for side in ("SELL","BUY"):
            for s in ASSETS:
                if terminal and side=="BUY":
                    continue
                p=pending.get(s)
                if p is None or p.side!=side:
                    continue
                if p.attempts>=ttl_opens:
                    cancel(s,"ttl_expired",index)
                    counts["expired"]+=1
                    continue
                price=opens[s][index]
                equity_now=nav(index)
                if side=="SELL":
                    requested=min(p.remaining_units,positions[s])
                else:
                    desire=0 if terminal else int(signals[s][decision])
                    if not desire:
                        cancel(s,"no_longer_desired",index)
                        continue
                    # Existing holdings drift naturally; no hidden daily
                    # rebalancing. New purchases cannot cross individual or
                    # aggregate limits after modeled fees/spread.
                    per_asset_room=max(0.,ASSET_TARGET_FRACTION*equity_now-
                                       positions[s]*price)
                    basket_room=max(0.,MAX_PORTFOLIO_ENTRY_FRACTION*equity_now-
                                    sum(positions[k]*opens[k][index] for k in ASSETS))
                    safety=1.+(fee_bps+spread_bps+impact_scale_bps)/10000.
                    requested=min(p.remaining_units,per_asset_room/price/safety,
                                  basket_room/price/safety)
                p.attempts+=1
                attempted_assets.add(s)
                if requested<=1e-12 or budgets[s]<=1e-12:
                    if budgets[s]<=1e-12:
                        unfilled_cap_attempts+=1
                        counts["zero_volume_attempts"]+=1
                    if requested<=1e-12 and side=="BUY":
                        cancel(s,"allocation_no_longer_available",index)
                    elif p.attempts>=ttl_opens:
                        cancel(s,"ttl_expired",index)
                        counts["expired"]+=1
                    continue
                max_by_capacity=budgets[s]/price
                units=min(requested,max_by_capacity)
                # An adverse open-price proxy only. No orderbook data exists.
                # Execution impact is determined from the requested (pre-fill)
                # notional and preceding completed notional history.
                impact_bps=spread_bps+impact_scale_bps*sqrt(min(
                    1.,requested*price/max(median[s],1e-12)
                ))
                execution_price=price*(1.+impact_bps/10000 if side=="BUY"
                                       else 1.-impact_bps/10000)
                if execution_price<=0:
                    raise ValueError("Invalid modeled executable price")
                fee_rate=fee_bps/10000
                if side=="BUY":
                    units=min(units,cash/(execution_price*(1.+fee_rate)))
                else:
                    units=min(units,positions[s])
                if units<=1e-12:
                    unfilled_cap_attempts+=1
                    if p.attempts>=ttl_opens:
                        cancel(s,"ttl_expired",index)
                        counts["expired"]+=1
                    continue
                mid_notional=units*price
                traded_notional=units*execution_price
                fee=traded_notional*fee_rate
                if side=="BUY":
                    cash-=traded_notional+fee
                    positions[s]+=units
                else:
                    cash+=traded_notional-fee
                    positions[s]-=units
                positions[s]=max(0.,positions[s])
                if cash < -1e-7:
                    raise ValueError("Margin used in partial fill")
                cash=max(0.,cash)
                budgets[s]=max(0.,budgets[s]-mid_notional)
                fee_paid+=fee
                estimated_price_impact_cost+=abs(traded_notional-mid_notional)
                p.remaining_units=max(0.,p.remaining_units-units)
                status="FILLED" if p.remaining_units<=1e-10 else "PARTIAL"
                fill_events.append({
                    "order_id":p.order_id,"date":data[s]["date"].iloc[index].date().isoformat(),
                    "open_index":index,"asset":s,"side":side,
                    "order_reason":p.reason,"fill_status":status,
                    "fill_units":float(units),"mid_open":float(price),
                    "assumed_execution_price":float(execution_price),
                    "market_notional_krw":float(mid_notional),
                    "cash_notional_krw":float(traded_notional),
                    "fee_krw":float(fee),"adverse_price_bps":float(impact_bps),
                    "remaining_order_units":float(p.remaining_units),
                    "volume_cap_prior_20d_krw":participation*median[s],
                })
                counts["fills"]+=1
                if status=="PARTIAL":
                    counts["partial_fills"]+=1
                    if p.attempts>=ttl_opens:
                        cancel(s,"ttl_expired",index)
                        counts["expired"]+=1
                else:
                    order_events.append({"order_id":p.order_id,"asset":s,
                                         "status":"FILLED","at_open_index":index})
                    pending.pop(s)
                # A partial fill can exhaust the per-day budget. Do not
                # overdraw it with any subsequent order for that asset.

        # Entries are opened after exit attempts. Orders are valid from
        # decision t only; sizing at open[t+1] is an optimistic first quote
        # model, not an advance knowledge of open[t+1].
        if not terminal:
            for s in ASSETS:
                if (int(signals[s][decision])!=1 or s in pending
                    or s in attempted_assets or positions[s]>1e-10):
                    # Never churn a held long merely to refill a trimmed
                    # allocation. Only the original partially filled order
                    # may continue, until cancellation or TTL expiry.
                    continue
                price=opens[s][index]
                account=nav(index)
                if gross(index)>MAX_PORTFOLIO_ENTRY_FRACTION+1e-9:
                    continue
                deficit=ASSET_TARGET_FRACTION*account-positions[s]*price
                if deficit<=ENTRY_REPLENISH_GAP_FRACTION*account:
                    continue
                room=min(deficit,MAX_PORTFOLIO_ENTRY_FRACTION*account-
                         sum(positions[k]*opens[k][index] for k in ASSETS))
                if room<=1e-8:
                    continue
                safety=1.+(fee_bps+spread_bps+impact_scale_bps)/10000.
                new_order(s,"BUY","signal_on",room/price/safety,decision,index)
            # execute freshly submitted entries at this opening with the same
            # causal past volume. Code separated to keep sell-before-buy.
            for s in ASSETS:
                p=pending.get(s)
                if not p or p.side!="BUY" or p.attempts>0:
                    continue
                price=opens[s][index]
                value=nav(index)
                capacity=budgets[s]
                target_room=max(0.,ASSET_TARGET_FRACTION*value-positions[s]*price)
                basket_room=max(0.,MAX_PORTFOLIO_ENTRY_FRACTION*value-
                                sum(positions[k]*opens[k][index] for k in ASSETS))
                safety=1.+(fee_bps+spread_bps+impact_scale_bps)/10000.
                req=min(p.remaining_units,target_room/price/safety,
                        basket_room/price/safety)
                p.attempts+=1
                attempted_assets.add(s)
                if capacity<=1e-12 or req<=1e-12:
                    if capacity<=1e-12:
                        counts["zero_volume_attempts"]+=1
                        unfilled_cap_attempts+=1
                    if p.attempts>=ttl_opens:
                        cancel(s,"ttl_expired",index);counts["expired"]+=1
                    continue
                impact_bps=spread_bps+impact_scale_bps*sqrt(min(
                    1.,req*price/max(median[s],1e-12)
                ))
                execution=price*(1.+impact_bps/10000)
                units=min(req,capacity/price,cash/(execution*(1.+fee_bps/10000)))
                if units<=1e-12:
                    unfilled_cap_attempts+=1
                    continue
                notional=units*execution
                fee=notional*fee_bps/10000
                cash-=notional+fee
                positions[s]+=units
                budgets[s]=max(0.,budgets[s]-units*price)
                fee_paid+=fee
                estimated_price_impact_cost+=notional-units*price
                p.remaining_units=max(0.,p.remaining_units-units)
                status="FILLED" if p.remaining_units<=1e-10 else "PARTIAL"
                fill_events.append({
                    "order_id":p.order_id,"date":data[s]["date"].iloc[index].date().isoformat(),
                    "open_index":index,"asset":s,"side":"BUY","order_reason":p.reason,
                    "fill_status":status,"fill_units":float(units),
                    "mid_open":float(price),"assumed_execution_price":float(execution),
                    "market_notional_krw":float(units*price),
                    "cash_notional_krw":float(notional),"fee_krw":float(fee),
                    "adverse_price_bps":float(impact_bps),
                    "remaining_order_units":float(p.remaining_units),
                    "volume_cap_prior_20d_krw":participation*median[s],
                })
                counts["fills"]+=1
                if status=="PARTIAL":
                    counts["partial_fills"]+=1
                    if p.attempts>=ttl_opens:
                        cancel(s,"ttl_expired",index);counts["expired"]+=1
                else:
                    order_events.append({"order_id":p.order_id,"asset":s,
                                         "status":"FILLED","at_open_index":index})
                    pending.pop(s)
        if cash < -1e-7 or any(q < -1e-10 for q in positions.values()):
            raise ValueError("Self-financing portfolio conservation violated")
        account=nav(index)
        if account<=0 or not np.isfinite(account):
            raise ValueError("Unbounded account losses")
        actual_gross=gross(index)
        max_post_trade_gross=max(actual_gross,max_post_trade_gross)
        if (actual_gross>MAX_PORTFOLIO_ENTRY_FRACTION+1e-8
            or any(positions[s]*opens[s][index]/account>MAX_HOLDING+1e-8
                   for s in ASSETS)):
            posttrade_breaches+=1

    # First decision at completed close[start] -> first eligible open[start+1].
    for t in range(start,end):
        process_open(t+1,t)
        marked=nav(t+2)
        if not np.isfinite(marked) or marked<=0:
            raise ValueError("Invalid intermediate mark")
        equity.append(float(marked))
        daily_path.append({
            "mark_date":data[ASSETS[0]]["date"].iloc[t+2].date().isoformat(),
            "marked_equity_krw":float(marked),"cash_krw":float(cash),
            "gross_fraction_at_mark":float(gross(t+2)),
            "unfilled_pending_count":len(pending),
        })
    # An additional terminal sell attempt at open[end+1], capacity-bound.
    # Holdings which cannot be sold stay ACTUAL marked residual positions.
    for s in list(pending):
        cancel(s,"end_of_research_horizon",end+1)
    process_open(end+1,end-1,terminal=True)
    for s in list(pending):
        cancel(s,"terminal_unfilled_residual",end+1)
    terminal_equity=nav(end+1)
    equity[-1]=float(terminal_equity)
    daily_path[-1]["marked_equity_krw"]=float(terminal_equity)
    daily_path[-1]["cash_krw"]=float(cash)
    daily_path[-1]["gross_fraction_at_mark"]=float(gross(end+1))
    daily_path[-1]["unfilled_pending_count"]=0

    curve=np.asarray(equity,dtype=float)
    changes=curve[1:]/curve[:-1]-1.
    maximum=np.maximum.accumulate(curve)
    std=float(changes.std(ddof=1)) if len(changes)>1 else 0.
    leftovers={s:float(q) for s,q in positions.items() if q>1e-10}
    return {
        "mode":"CAUSAL_DAILY_PROXY_PARTIAL_FILL_ACCOUNT_V1",
        "status":"RESIDUAL_NOT_LIQUIDATED" if leftovers else "RESEARCH_ONLY_NOT_EXECUTABLE_EVIDENCE",
        "assets":list(ASSETS),
        "initial_cash_krw":float(initial),"final_cash_krw":float(cash),
        "final_marked_equity_krw":float(terminal_equity),
        "total_marked_return":float(terminal_equity/initial-1),
        "worst_marked_drawdown":float((curve/maximum-1).min()),
        "sharpe_daily_365":float(changes.mean()/std*np.sqrt(365)) if std>1e-12 else 0.,
        "fee_paid_krw":float(fee_paid),
        "adverse_price_proxy_cost_krw":float(estimated_price_impact_cost),
        "equity_path":daily_path,
        "hypothetical_partial_fills":fill_events,"order_lifecycle":order_events,
        "counts":{key:int(counts[key]) for key in (
            "created","fills","partial_fills","cancelled",
            "expired","zero_volume_attempts")},
        "unfilled_zero_capacity_attempts":unfilled_cap_attempts,
        "residual_units_not_cash":leftovers,
        "pending_order_count_at_end":0,
        "pretrade_gross_breach_count":pretrade_breaches,
        "posttrade_risk_breach_count":posttrade_breaches,
        "max_posttrade_gross_fraction":float(max_post_trade_gross),
        "participation_fraction_of_trailing_20d_daily_notional":participation,
        "fee_bps_per_side":fee_bps,"base_adverse_spread_bps":spread_bps,
        "impact_sqrt_bps_scale":impact_scale_bps,
        "ttl_opens":ttl_opens,
        "synthetic_quote_no_actual_orderbook":True,
        "daily_total_volume_not_assumed_available_at_open":True,
        "risk_limit_cannot_be_guaranteed_during_zero_volume":True,
        "real_orders_enabled":False,"automatic_strategy_promotion":False,
        "eligible_for_real_trading":False,
    }


def evaluate_saved(*,cross_snapshot:str|Path,historical_snapshot:str|Path,
                   output_dir:str|Path,state_dir:str|Path,
                   sizes:tuple[float,...]=(1_000_000.,100_000_000.,1_000_000_000.),
                   participation_tiers:tuple[float,...]=(.0025,.01),
                   include_qlib:bool=True)->dict:
    output=Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Append-only research report required")
    frames,provenance=load_crypto_snapshots(cross_snapshot,historical_snapshot)
    strategies=policies(frames)
    fit_proofs=[]
    if include_qlib:
        qlib,fit_proofs=qlib_causal_signals(frames,state_dir=state_dir)
        strategies["qlib_causal_stitched"]=qlib
    results={}
    n=len(frames[ASSETS[0]])
    for name,decision in strategies.items():
        results[name]={}
        for size in sizes:
            results[name][str(int(size))]={}
            for limit in participation_tiers:
                results[name][str(int(size))][str(limit)]=fill_aware_account(
                    frames,decision,START,n-2,
                    initial_cash=float(size),participation=float(limit))
    report={
        "status":"EXPLORATORY_FILL_AWARE_EXECUTION_PROXY",
        "created_at_utc":datetime.now(timezone.utc).isoformat(),
        "data_provenance":provenance,
        "orderbook_verified":False,"partial_fill_model_calibrated":False,
        "future_prices_used_for_signal_selection":False,
        "Qlib_model_training_only_matured_past_labels":True,
        "new_fresh_unseen_holdout":False,
        "strategies":results,"qlib_training_boundaries":fit_proofs,
        "real_orders_enabled":False,"automatic_strategy_promotion":False,
    }
    output.mkdir(parents=True,exist_ok=True)
    _safe_report(output/"results.json",report)
    return report


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="Research-only partial fill and capacity-sensitive account")
    p.add_argument("--cross-snapshot",default="/home/hong/apps/FreeQuantLab-crossmarket-20261021/reports/cross-market-snapshot-20261021")
    p.add_argument("--historical-snapshot",default="/home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021")
    p.add_argument("--output-dir",default="reports/partial-fill-study-20261021")
    p.add_argument("--state-dir",default="state/qlib_partial_fill_research")
    p.add_argument("--without-qlib",action="store_true")
    args=p.parse_args(argv)
    result=evaluate_saved(
        cross_snapshot=args.cross_snapshot,historical_snapshot=args.historical_snapshot,
        output_dir=args.output_dir,state_dir=args.state_dir,
        include_qlib=not args.without_qlib,
    )
    print(json.dumps({
        "status":result["status"],"report":str(Path(args.output_dir)/"results.json"),
        "results":{
            name:{size:{tier:{k:r[k] for k in (
                "total_marked_return","worst_marked_drawdown","fee_paid_krw",
                "adverse_price_proxy_cost_krw","posttrade_risk_breach_count",
                "residual_units_not_cash")}
                for tier,r in tiers.items()} for size,tiers in scales.items()}
            for name,scales in result["strategies"].items()
        },
        "real_orders_enabled":False,
    },ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

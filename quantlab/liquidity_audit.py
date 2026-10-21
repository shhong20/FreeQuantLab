"""Ex-post historical order-capacity and adverse execution cost envelope.

This is an *audit* of previously simulated intents, not an adjusted portfolio
backtest. A failed capacity cap means the old backtest requires a different
(fill-aware) state path: NEVER keep its profit while pretending the blocked
order filled. Volume is in BASE units for Upbit daily candles.

At the previous decision close, trailing median of past 20 completed daily
notionals is available. Full next-day realized volume is an additional EX POST
capacity check, never usable as a prospective decision input. Neither is
tick-by-tick executable orderbook depth; PASS is not proof of real liquidity.
"""
from __future__ import annotations

from collections import defaultdict
from math import sqrt
from pathlib import Path
import json
import numpy as np
import pandas as pd

from .continuous_portfolio import ASSETS, verify_aligned_crypto, load_crypto_snapshots
from .daily_receipt import _safe_report

PARTICIPATION_LIMITS=(0.0025,0.01,0.05)
CAPITAL_MULTIPLIERS=(1,10,100,1000)
BASE_SPREAD_BPS=5.
IMPACT_SCALE_BPS=40.
VOL_LOOKBACK=20


def audit_trace(frames:dict[str,pd.DataFrame],trace:dict, *,
                participation:float=.01,
                base_spread_bps:float=BASE_SPREAD_BPS,
                impact_scale_bps:float=IMPACT_SCALE_BPS)->dict:
    data=verify_aligned_crypto(frames)
    if (not np.isfinite(participation) or not 0<participation<=.1 or
        not np.isfinite(base_spread_bps) or base_spread_bps<0 or
        not np.isfinite(impact_scale_bps) or impact_scale_bps<0):
        raise ValueError("Invalid conservative execution scenario")
    if trace.get("mode")!="ONE_CONTINUOUS_ACCOUNT_NO_FOLD_RESETS" or not trace.get("no_real_orders"):
        raise ValueError("Refuse non-research or incomplete account trace")
    orders=trace.get("hypothetical_fills")
    if not isinstance(orders,list) or trace.get("orders")!=len(orders):
        raise ValueError("Invalid hypothetical order evidence")
    sessions={}
    trailing={}
    for asset in ASSETS:
        df=data[asset]
        sessions[asset]={x.date().isoformat():i for i,x in enumerate(df["date"])}
        notional=df["close"].to_numpy(dtype=float)*df["volume"].to_numpy(dtype=float)
        if not np.isfinite(notional).all() or np.any(notional<0):
            raise ValueError("Invalid OHLCV market notional")
        trailing[asset]=notional
    used=defaultdict(float)
    failing=[]
    cost_increment=0.
    size_ratios=[]
    fee_rate=float(trace.get("assumed_cost_bps_per_side",-1))
    if not 0<=fee_rate<=1000:
        raise ValueError("Invalid original cost assumption")
    fees_check=0.
    order_keys=set()
    for ix,event in enumerate(orders):
        if not isinstance(event,dict) or event.get("asset") not in ASSETS or event.get("side") not in ("BUY","SELL"):
            raise ValueError("Invalid broker-intent-like record")
        asset=event["asset"]
        day=event.get("date")
        ti=sessions[asset].get(day)
        if ti is None or ti<VOL_LOOKBACK+1:
            raise ValueError("Trade without past volume evidence")
        qty=float(event["quantity"])
        notional=float(event["notional"])
        price=float(data[asset]["open"].iloc[ti])
        if (not np.isfinite(qty) or not np.isfinite(notional) or
            qty<=0 or notional<=0 or not np.isclose(qty*price,notional,rtol=1e-8,atol=1e-5)):
            raise ValueError("Order notional cannot reconcile to recorded next open")
        actual_fee=float(event["cost"])
        if not np.isfinite(actual_fee) or not np.isclose(actual_fee,notional*fee_rate/10000,rtol=1e-7,atol=1e-4):
            raise ValueError("Original simulated cost does not reconcile")
        fees_check+=actual_fee
        past=trailing[asset][ti-VOL_LOOKBACK:ti]
        expected_daily=float(np.median(past))
        actual_daily=float(data[asset]["volume"].iloc[ti]*price)
        if not np.isfinite(expected_daily) or expected_daily<=0 or not np.isfinite(actual_daily):
            raise ValueError("Missing or invalid fill-day/ex-post market volume")
        used[(asset,day)]+=notional
        cumulative=used[(asset,day)]
        cap=participation*min(expected_daily,actual_daily)
        ratio=cumulative/min(expected_daily,actual_daily) if actual_daily>0 else float("inf")
        if not np.isfinite(ratio):
            ratio=1e300
        size_ratios.append(ratio)
        # This is a stress cost ENVELOPE, not a realized cost in the actual
        # original equity path. No adjusted ROI is output from this audit.
        extra_bps=base_spread_bps+impact_scale_bps*sqrt(
            min(1.,notional/expected_daily)
        )
        cost_increment+=notional*extra_bps/10000
        if cap <= 0 or cumulative>cap+1e-7:
            failing.append({
                "event_index":ix, "asset":asset,"date":day,"side":event["side"],
                "order_notional_krw":notional,
                "cumulative_same_day_notional_krw":cumulative,
                "max_conservative_notional_krw":cap,
                "past_20d_median_daily_notional_krw":expected_daily,
                "ex_post_next_day_daily_notional_krw":actual_daily,
            })
    if not np.isclose(fees_check,float(trace["paid_fees_and_slippage_krw"]),atol=1e-6,rtol=1e-8):
        raise ValueError("Hypothetical order ledger fees not conserved")
    return {
        "status":"NOT_EXECUTABLE_AT_CAP" if failing else "BOUNDED_HISTORICAL_VOLUME_SCREEN_ONLY",
        "participation_cap_fraction":participation,
        "trailing_median_sessions":VOL_LOOKBACK,
        "volume_source":"Upbit complete UTC daily base-asset volume; next-day realized volume EX POST",
        "limit_rule":"same-day cumulative trade notional <= cap * MIN(prior20_median_notional, next_day_actual_notional)",
        "orders_checked":len(orders),"failed_orders":len(failing),
        "failure_examples":failing[:20],
        "max_cumulative_share_of_conservative_daily_volume":float(max(size_ratios,default=0.)),
        "additional_spread_bps_per_side":base_spread_bps,
        "additional_sqrt_impact_bps_scale":impact_scale_bps,
        "estimated_additional_cost_krw_if_ALL_original_orders_filled":float(cost_increment),
        "assumed_original_cost_bps_per_side":fee_rate,
        "original_equity_performance_valid_under_this_screen":not bool(failing),
        "stress_cost_not_applied_to_original_account_equity":True,
        "new_net_roi_not_computed":True,
        "capacity_screen_not_proof_of_executable_depth_or_exact_slippage":True,
        "auto_promotion_allowed":False,"real_orders_enabled":False,
    }


def proportionally_scaled_order_trace(trace:dict, multiplier:int)->dict:
    """Capacity-only proportional ticket sizing. NOT a larger account backtest.

    Price path, target decisions and risk allocation are copied; absolute
    order sizes grow. Trades that fail capacity are reported as unfilled.
    No scaled ROI is created or implied from this hypothetical stress case.
    """
    import copy
    if type(multiplier) is not int or multiplier not in CAPITAL_MULTIPLIERS:
        raise ValueError("Unregistered stress capital multiplier")
    replica=copy.deepcopy(trace)
    if replica.get("mode")!="ONE_CONTINUOUS_ACCOUNT_NO_FOLD_RESETS":
        raise ValueError("Unknown research order trace")
    for event in replica["hypothetical_fills"]:
        for key in ("quantity","notional","cost"):
            event[key]*=multiplier
    replica["paid_fees_and_slippage_krw"]*=multiplier
    replica["capacity_only_hypothetical_initial_cash_krw"]=float(trace["start_initial_cash_krw"])*multiplier
    replica["not_a_recomputed_capital_return"]=True
    return replica


def audit_saved(*,continuous_report:str|Path,
                cross_snapshot:str|Path,
                historical_snapshot:str|Path,
                output_dir:str|Path)->dict:
    root=Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Refuse overwritten liquidity audit")
    original=json.loads(Path(continuous_report).read_text())
    if original.get("status")!="EXPLORATORY_CONTINUOUS_CRYPTO_PORTFOLIO_ONLY":
        raise ValueError("Unknown original trace report")
    frames,prov=load_crypto_snapshots(cross_snapshot,historical_snapshot)
    outputs={}
    for strategy,cases in original["strategy_results"].items():
        outputs[strategy]={}
        for fee,trace in cases.items():
            outputs[strategy][fee]={
                str(multiplier):{
                    str(participation):audit_trace(
                        frames,proportionally_scaled_order_trace(trace,multiplier),
                        participation=participation,
                    )
                    for participation in PARTICIPATION_LIMITS
                }
                for multiplier in CAPITAL_MULTIPLIERS
            }
    report={
        "kind":"historic_daily_volume_liquidity_audit_v2",
        "original_report":str(continuous_report),
        "input_hashes":{s:p["sha256"] for s,p in prov.items()},
        "scenario_rates":list(PARTICIPATION_LIMITS),
        "capital_multipliers":list(CAPITAL_MULTIPLIERS),
        "capital_scale_only_not_recomputed_equity":True,
        "results":outputs,
        "full_fill_path_recomputed":False,
        "real_orders_enabled":False,
        "automatic_strategy_promotion":False,
    }
    root.mkdir(parents=True,exist_ok=True)
    _safe_report(root/"results.json",report)
    return report


def main(argv=None)->int:
    import argparse
    p=argparse.ArgumentParser(description="Volume stress audit of existing hypothetical fills")
    p.add_argument("--report",default="/home/hong/apps/FreeQuantLab-continuous-20261021/reports/continuous-crypto-full-20261021/results.json")
    p.add_argument("--cross-snapshot",default="/home/hong/apps/FreeQuantLab-crossmarket-20261021/reports/cross-market-snapshot-20261021")
    p.add_argument("--historical-snapshot",default="/home/hong/apps/FreeQuantLab-historical-20261021/reports/historical-stress-20261021")
    p.add_argument("--output-dir",default="reports/liquidity-audit-20261021")
    args=p.parse_args(argv)
    result=audit_saved(
        continuous_report=args.report,cross_snapshot=args.cross_snapshot,
        historical_snapshot=args.historical_snapshot,output_dir=args.output_dir)
    print(json.dumps({
        "kind":result["kind"],"output":args.output_dir,
        "rejection_counts":{
            strategy:{
                cost:{multiple:{scenario:r["failed_orders"] for scenario,r in tiers.items()}
                      for multiple,tiers in by_size.items()}
                for cost,by_size in cases.items()
            } for strategy,cases in result["results"].items()
        },
        "no_real_orders":True,"no_adjusted_ROI_computed":True,
    },ensure_ascii=False))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

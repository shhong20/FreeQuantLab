"""Read-only Upbit public recent market trade prints (NOT our fills).

Trade prints show trades that actually occurred, not queue position or a
counterfactual fill for an unsubmitted order. REST history is limited to
recent dates. Proofs are immutable SHA-256 receipts, never authentication.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import requests
import numpy as np

from .continuous_portfolio import ASSETS
from .daily_receipt import _safe_report

URL="https://api.upbit.com/v1/trades/ticks"
COUNT=200


def _time(value):
    return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc)


def check_trade_prints(trades,market:str,now:datetime):
    if not isinstance(trades,list) or not trades or len(trades)>COUNT:
        raise ValueError("Invalid recent public prints")
    ids=set()
    times=[]
    for rec in trades:
        if not isinstance(rec,dict) or rec.get("market")!=market:
            raise ValueError("Wrong market public trade")
        if rec.get("ask_bid") not in ("ASK","BID"):
            raise ValueError("Unknown trade aggressor direction")
        id=rec.get("sequential_id")
        if type(id)!=int or id in ids:
            raise ValueError("Duplicate/invalid market trade ID")
        ids.add(id)
        stamp=rec.get("timestamp")
        if type(stamp)!=int:
            raise ValueError("Unknown trade timestamp")
        age=(now-datetime.fromtimestamp(stamp/1000,timezone.utc)).total_seconds()
        if age < -3 or age > 7*86400:
            raise ValueError("Stale/future public trade")
        for key in ("trade_price","trade_volume"):
            value=rec.get(key)
            if type(value) not in (int,float) or not np.isfinite(value) or value<=0:
                raise ValueError("Invalid public trade price/quantity")
        times.append(stamp)
    latest_age=(now-datetime.fromtimestamp(max(times)/1000,timezone.utc)).total_seconds()
    if latest_age>600:
        raise ValueError("No sufficiently recent market trades")
    return {
        "market":market,"trades":len(trades),
        "latest_age_seconds":latest_age,
        "trade_window_seconds":(max(times)-min(times))/1000.,
        "first_timestamp_ms":min(times),"latest_timestamp_ms":max(times),
        "buy_aggressor_trades":sum(rec["ask_bid"]=="BID" for rec in trades),
        "sell_aggressor_trades":sum(rec["ask_bid"]=="ASK" for rec in trades),
        "sum_print_notional_krw":sum(rec["trade_price"]*rec["trade_volume"] for rec in trades),
        "trade_prints_not_customer_order_execution":True,
    }


def collect_trade_prints(output_dir:str|Path,*,session=requests)->dict:
    root=Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Refuse overwritten trade prints")
    root.mkdir(parents=True,exist_ok=True)
    evidence=[]
    for market in ASSETS:
        requested=datetime.now(timezone.utc)
        response=session.get(URL,params={"market":market,"count":COUNT},
                             timeout=(3,7),headers={
                                 "Accept":"application/json",
                                 "User-Agent":"FreeQuantLab-Research/0.2 readonly",
                             })
        response.raise_for_status()
        received=datetime.now(timezone.utc)
        if (received-requested).total_seconds()>8:
            raise ValueError("Public trade fetch too slow")
        raw=response.content
        if not raw or len(raw)>250_000:
            raise ValueError("Malformed public trade API response")
        parsed=json.loads(raw)
        quality=check_trade_prints(parsed,market,received)
        file=f"prints-{market}.json"
        (root/file).write_bytes(raw)
        evidence.append({
            "market":market,"file":file,"sha256":sha256(raw).hexdigest(),
            "requested_utc":requested.isoformat(),
            "received_utc":received.isoformat(),**quality,
        })
    report={
        "status":"RECENT_PUBLIC_MARKET_TRADES_NOT_OUR_FILLS",
        "endpoint":URL,"capture_count":len(evidence),
        "markets":evidence,
        "no_private_apis":True,"no_actual_orders":True,
        "past_trades_not_proof_of_counterfactual_fill":True,
        "trade_quote_time_alignment_not_proven":True,
        "real_orders_enabled":False,
    }
    _safe_report(root/"manifest.json",report)
    return report


def audit_saved_prints(folder:str|Path)->dict:
    root=Path(folder)
    manifest=json.loads((root/"manifest.json").read_text())
    if manifest.get("status")!="RECENT_PUBLIC_MARKET_TRADES_NOT_OUR_FILLS" or set(
        x["market"] for x in manifest["markets"])!=set(ASSETS
    ):
        raise ValueError("Unknown/incomplete public trade receipt")
    for evidence in manifest["markets"]:
        raw=(root/evidence["file"]).read_bytes()
        if sha256(raw).hexdigest()!=evidence["sha256"]:
            raise ValueError("Trade SHA mismatch")
        parsed=json.loads(raw)
        quality=check_trade_prints(parsed,evidence["market"],_time(evidence["received_utc"]))
        for key,v in quality.items():
            if v!=evidence[key]:
                raise ValueError("Trade receipt summary mismatch")
    return {
        "status":"PUBLIC_TRADE_PRINTS_HASHES_VERIFIED",
        "count":len(manifest["markets"]),
        "no_own_order_fills":True,
    }


def main(argv=None):
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument("--output-dir",default="reports/public-trade-prints-20261021")
    p.add_argument("--audit-only",action="store_true")
    args=p.parse_args(argv)
    if not args.audit_only:
        collect_trade_prints(args.output_dir)
    print(json.dumps(audit_saved_prints(args.output_dir),ensure_ascii=False))
    return 0

if __name__=="__main__":
    raise SystemExit(main())

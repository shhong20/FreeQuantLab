"""Issuer evidence registry and historical ETF total-return readiness gate.

Official product-page verification proves identity and payout POLICY, *not*
complete distribution event history, ex-dates, split factors, adjusted
execution prices or tax cash flows. No missing events are silently set to
zero. ETF total-return backtesting remains blocked without issuer-verified
event-level provenance and independently reviewed corporate actions.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import re
import requests

from .daily_receipt import _safe_report
from .etf_price_audit import audit_etf_snapshot

PRODUCTS={
    "069500":{"product_id":"2ETF01","official_name":"KODEX 200","category":"KOSPI200 spot tracking"},
    "114800":{"product_id":"2ETF20","official_name":"KODEX 인버스","category":"daily inverse F-KOSPI200 futures"},
    "132030":{"product_id":"2ETF24","official_name":"KODEX 골드선물(H)","category":"currency-hedged rolling gold futures"},
}
ROOT="https://m.samsungfund.com/etf/product/view.do?id="
MAX_BODY_BYTES=500_000


def fetch_official_evidence(*,fetcher=requests.get)->dict:
    found={}
    for ticker,meta in PRODUCTS.items():
        url=ROOT+meta["product_id"]
        response=fetcher(url,timeout=12,headers={"User-Agent":"FreeQuantLab/0.1 public research"})
        response.raise_for_status()
        # Avoid silently accepting a redirect to an unrelated document
        # or a different fund page; only an official Korean issuer host.
        final=getattr(response,"url",url)
        from urllib.parse import urlsplit
        host=urlsplit(final)
        if host.scheme!="https" or host.hostname not in ("www.samsungfund.com","m.samsungfund.com"):
            raise ValueError("Issuer response redirected outside official domain")
        body=response.content
        if not body or len(body)>MAX_BODY_BYTES:
            raise ValueError("Unexpected official ETF product response length")
        html=body.decode("utf-8")
        label=re.search(r"<title[^>]*>(.*?)</title>",html,re.I|re.S)
        if not label or meta["official_name"] not in label.group(1) or ticker not in html:
            raise ValueError("Official issuer ticker/product mismatch: "+ticker)
        if "분배금" not in html:
            raise ValueError("Issuer product page lacks distribution information")
        found[ticker]={
            "official_url":url,"verified_url":final,
            "official_name":meta["official_name"],
            "category":meta["category"],"html_sha256":sha256(body).hexdigest(),
            "html_bytes":len(body),"issuer_ticker_identity_verified":True,
            "payout_policy_present":True,
            "full_historical_cash_distributions_verified":False,
            "split_and_adjustment_factor_verified":False,
            "historical_total_return_ready":False,
        }
    return {"as_of_utc":datetime.now(timezone.utc).isoformat(),
            "status":"PARTIAL_OFFICIAL_ISSUER_IDENTITY_ONLY",
            "official_issuer":"Samsung Asset Management Kodex",
            "products":found,"real_orders_enabled":False,
            "eligible_for_etf_total_return_backtest":False}


def integrate_evidence(snapshot_dir:str|Path, issuer:dict)->dict:
    if issuer.get("status")!="PARTIAL_OFFICIAL_ISSUER_IDENTITY_ONLY":
        raise ValueError("Unrecognized official issuer evidence")
    if set(issuer.get("products",{}))!=set(PRODUCTS):
        raise ValueError("Incomplete official ETF identity evidence")
    audit=audit_etf_snapshot(snapshot_dir)
    for ticker,evidence in issuer["products"].items():
        if not evidence.get("issuer_ticker_identity_verified"):
            raise ValueError("Unknown official issuer identity")
        audit["funds"][ticker]["issuer_product_id_verified"]=True
        audit["funds"][ticker]["issuer_product_page_url"]=evidence["official_url"]
        audit["funds"][ticker]["official_page_sha256"]=evidence["html_sha256"]
        audit["funds"][ticker]["issuer_distribution_policy_exists"]=True
    audit["official_product_identity_verified"]=True
    audit["official_complete_distribution_events_verified"]=False
    audit["official_full_adjusted_market_ohlcv_verified"]=False
    audit["status"]="BLOCKED_TOTAL_RETURN_VERIFICATION"
    audit["total_return_performance_publishable"]=False
    audit["automatic_strategy_promotion"]=False
    return audit


def save_evidence(snapshot_dir:str|Path, report_dir:str|Path)->dict:
    target=Path(report_dir)
    if target.exists() and any(target.iterdir()):
        raise ValueError("Issuer audit must never overwrite earlier evidence")
    issuer=fetch_official_evidence()
    audit=integrate_evidence(snapshot_dir,issuer)
    target.mkdir(parents=True,exist_ok=True)
    _safe_report(target/"issuer_pages.json",issuer)
    _safe_report(target/"audit.json",audit)
    return {"status":audit["status"],"report_dir":str(target),
            "products":list(audit["funds"]),
            "fully_verified":False,"automatic_strategy_promotion":False}

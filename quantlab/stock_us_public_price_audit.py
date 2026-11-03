"""Two public display-page OHLC references for 5 unresolved US stock sessions.

Capture six read-only, fixed-URL HTML snapshots. Each report independently
rechecks original SQLite evidence, immutable raw vendor SHAs, both captured
HTML parses, and SHA-pinned manifest. Cent-displayed quotations are NEVER
exchange-certified executions or permission to fill missing backtest history.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlparse

import requests

from .stock_etf_distribution_study import _checked_gap_report
from .stock_gap_provenance import DEFAULT_OBSERVATIONS, DEFAULT_HISTORY

VERSION="US_EOD_PUBLIC_DISPLAY_CROSS_REFERENCE_V1_20261103"
STATUS="US_STOCK_PUBLIC_PRICE_COMPARISON_ONLY_NO_EXCHANGE_EXECUTION_CERTIFICATION"
SYMBOL_DATES={
    "AAPL":("2026-10-22","2026-10-27"),
    "MSFT":("2026-10-22","2026-10-27"),
    "SPY":("2026-10-27",),
}
PROVIDERS=("stockanalysis","yahoo")
SOURCE_URLS={
    ("stockanalysis","AAPL"):"https://stockanalysis.com/stocks/aapl/history/",
    ("stockanalysis","MSFT"):"https://stockanalysis.com/stocks/msft/history/",
    ("stockanalysis","SPY"):"https://stockanalysis.com/etf/spy/history/",
    ("yahoo","AAPL"):"https://finance.yahoo.com/quote/AAPL/history/",
    ("yahoo","MSFT"):"https://finance.yahoo.com/quote/MSFT/history/",
    ("yahoo","SPY"):"https://finance.yahoo.com/quote/SPY/history/",
}
PUBLIC_DISPLAY_DECIMALS=2
RAW_LIMIT_BYTES=1_500_000
FILES=("report.json","report.sha256","manifest.json","manifest.sha256")
HEADERS={"User-Agent":"Mozilla/5.0 (compatible; HistoricalPriceEvidence/1.0)"}


class _PriceTable(HTMLParser):
    """Minimal stdlib HTML table parser; ignores script and non-table text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth=0
        self._in_row=False
        self._cell_depth=0
        self._cell_parts:list[str]=[]
        self._row:list[str]=[]
        self.tables:list[list[list[str]]]=[]
    def handle_starttag(self,tag,attrs):
        if tag=="table":
            self._depth+=1
            if self._depth==1:
                self.tables.append([])
        elif self._depth==1 and tag=="tr":
            self._row=[]
            self._in_row=True
        elif self._in_row and tag in ("td","th"):
            self._cell_depth+=1
            if self._cell_depth==1:
                self._cell_parts=[]
    def handle_data(self,data):
        if self._cell_depth:
            self._cell_parts.append(data)
    def handle_endtag(self,tag):
        if tag in ("td","th") and self._cell_depth:
            self._cell_depth-=1
            if not self._cell_depth:
                self._row.append(" ".join("".join(self._cell_parts).split()))
        elif tag=="tr" and self._in_row:
            if self._row and self.tables:
                self.tables[-1].append(self._row)
            self._in_row=False
        elif tag=="table" and self._depth:
            self._depth-=1


def _sha(data:bytes)->str:
    return sha256(data).hexdigest()


def _parse_html(data:bytes,*,provider:str,ticker:str,days:tuple[str,...])->dict:
    if (not data or len(data)>RAW_LIMIT_BYTES or
        provider not in PROVIDERS or ticker not in SYMBOL_DATES):
        raise ValueError("Unexpected price HTML or market identifier")
    html=data.decode("utf-8")
    if ticker not in html[:100_000]:
        raise ValueError("Ticker identity absent in provider response")
    parser=_PriceTable()
    parser.feed(html)
    candidates=[]
    for table in parser.tables:
        if not table:
            continue
        header=[v.lower() for v in table[0][:5]]
        if (len(header)!=5 or header[:4]!=["date","open","high","low"] or
            not header[4].startswith("close")):
            continue
        candidates.append(table)
    if len(candidates)!=1:
        raise ValueError("Missing or ambiguous historical OHLC table")
    table=candidates[0]
    found={}
    wanted={}
    for day in days:
        dt=datetime.strptime(day,"%Y-%m-%d")
        wanted[f"{dt.strftime('%b')} {dt.day}, {dt.year}"]=day
    for row in table[1:]:
        if not row or row[0] not in wanted:
            continue
        if len(row)<5:
            raise ValueError("Truncated OHLC row")
        day=wanted[row[0]]
        if day in found:
            raise ValueError("Duplicate displayed market day")
        vals=[]
        for cell in row[1:5]:
            raw=cell.strip().replace(",","")
            val=Decimal(raw)
            if (not val.is_finite() or val<=0 or
                val.as_tuple().exponent < -PUBLIC_DISPLAY_DECIMALS):
                raise ValueError("OHLC display not exactly cent precision")
            vals.append(str(val.quantize(Decimal("0.01"))))
        # Numeric OHLC bounds, not lexicographic string comparisons.
        v=[Decimal(z) for z in vals]
        if v[2]>min(v[0],v[3]) or v[1]<max(v[0],v[3]):
            raise ValueError("Invalid public OHLC range")
        found[day]=vals
    if set(found)!=set(days):
        raise ValueError("Missing required US trading day")
    return dict(sorted(found.items()))


def _cent(f:float)->str:
    if type(f) not in (float,int) or not math.isfinite(f) or f<=0:
        raise ValueError("Invalid original source price")
    return str(Decimal(str(f)).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))


def _near_half_cent(f:float)->bool:
    cents=f*100
    return abs(cents-(math.floor(cents)+.5))<=.002


def _classify(prior:list[float],common:list[str])->str:
    diff=[abs(float(x)-float(y)) for x,y in zip(prior,common)]
    if all(_cent(x)==p for x,p in zip(prior,common)):
        return "ORIGINAL_MATCHES_PUBLIC_CENTS_PRECISION_NOT_EXCHANGE_CERTIFIED"
    if max(diff)<=.006 and any(_near_half_cent(x) for x in prior):
        return "ORIGINAL_NEAR_HALF_CENT_TIE_AMBIGUOUS_NOT_RESOLVED"
    if max(diff)<=.015:
        return "ORIGINAL_ONE_CENT_PRICE_DISAGREEMENT_NOT_RESOLVED"
    return "MATERIAL_ORIGINAL_PRICE_DISAGREEMENT_NOT_RESOLVED"


def _rows(gap:dict,captures:dict[tuple[str,str],dict])->list[dict]:
    if (gap.get("status")!="VENDOR_HISTORY_GAP_PROVENANCE_UNRESOLVED_NO_TRADING" or
        gap.get("broker_orders")!=0 or
        gap.get("paper_or_live_trading_eligible") is not False):
        raise ValueError("Original history evidence untrusted")
    us=[x for x in gap["gaps"] if x.get("market")=="NASDAQ_NYSE"]
    if {(x["symbol"],x["session"]) for x in us}!={
        (sy,day) for sy,days in SYMBOL_DATES.items() for day in days} or len(us)!=5:
        raise ValueError("Unexpected US gap population")
    rows=[]
    for gap_row in sorted(us,key=lambda r:(r["symbol"],r["session"])):
        sy=gap_row["symbol"]; day=gap_row["session"]
        earliest=gap_row["sources"][0]
        first=[float(v) for v in earliest["observed_ohlc"]]
        latest=[float(v) for v in earliest["vendor_ohlc"]]
        sites={provider:captures[(provider,sy)]["days"][day] for provider in PROVIDERS}
        match=sites["stockanalysis"]==sites["yahoo"]
        public=sites["stockanalysis"]
        vend_match=all(_cent(x)==z for x,z in zip(latest,public)) if match else False
        rows.append({
            "symbol":sy,"market":"NASDAQ_NYSE","session":day,
            "source_conflict_classification":gap_row["latest_conflict_classification"],
            "parent_vendor_raw_sha256":earliest["raw_sha256"],
            "original_observed_ohlc":[float(x) for x in first],
            "latest_vendor_ohlc":[float(x) for x in latest],
            "source_displayed_ohlc_usd":sites,
            "two_public_sites_same_displayed_cent_ohlc":match,
            "later_vendor_rounds_to_public_cents":vend_match,
            "original_display_price_classification":(
                _classify(first,public) if match else "PUBLIC_WEB_PAGES_DISAGREE"),
            "original_observed_near_half_cent_boundary":any(
                _near_half_cent(x) for x in first),
            "quote_precision":"USD 0.01 public display only",
            "public_pages_are_not_independent_exchange_prints":True,
            "vendor_source_independence_not_established":True,
            "broker_fill_evidence":False,
            "price_gap_auto_resolved":False,
            "canonical_history_override":False,
            "model_training_backtest_paper_live_allowed":False,
        })
    return rows


def _derive(gap:dict,gap_sha:str,
            raw:dict[tuple[str,str],bytes],captured_at:str)->tuple[dict,dict]:
    dt=datetime.fromisoformat(captured_at)
    if dt.utcoffset() is None:
        raise ValueError("Capture timestamp must be timezone aware")
    captured={}
    meta={}
    for sy,days in SYMBOL_DATES.items():
        for provider in PROVIDERS:
            key=(provider,sy)
            payload=raw[key]
            filename=f"{provider}_{sy}.html"
            parsed=_parse_html(payload,provider=provider,ticker=sy,days=days)
            captured[key]={"days":parsed}
            meta[filename]={
                "source":provider,"symbol":sy,
                "url":SOURCE_URLS[key],
                "sha256":_sha(payload),"bytes":len(payload),
                "exact_trading_days":list(days),
            }
    records=_rows(gap,captured)
    report={
        "schema":"US_PUBLIC_DAILY_CROSS_REFERENCE_V1_20261103",
        "status":STATUS,"captured_at_utc":captured_at,
        "source_gap_provenance_sha256":gap_sha,
        "original_stocks_verified_from_readonly_immutable_source_dbs":True,
        "comparisons":records,
        "checked_conflict_sessions":len(records),
        "public_sites_agree_at_displayed_cents":sum(
            row["two_public_sites_same_displayed_cent_ohlc"] for row in records),
        "later_vendor_matches_both_displayed_cents":sum(
            row["later_vendor_rounds_to_public_cents"] for row in records),
        "original_market_price_corrections_committed":0,
        "historical_strategy_return":None,
        "exchange_grade_executions_verified":False,
        "historical_market_data_complete":False,
        "90d_clean_cohort_changed":False,
        "broker_orders":0,
        "backtest_paper_live_approved":False,
        "independent_exchange_tape_needed":True,
    }
    report_payload=(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
    manifest={
        "schema":VERSION,"status":STATUS,
        "captured_at_utc":captured_at,
        "stock_gap_source_sha256":gap_sha,
        "raw_pages":dict(sorted(meta.items())),
        "report_sha256":_sha(report_payload),
        "report_bytes":len(report_payload),
        "source_html_is_unofficial_not_exchange_certified":True,
        "no_trading_order_endpoints_used":True,
        "historical_vendor_prices_not_replaced":True,
    }
    return manifest,report


def _pinned_gap(gap_path:Path,obs:Path,hist:Path):
    return _checked_gap_report(gap_path,obs,hist)


def _load_raw(path:Path)->dict[tuple[str,str],bytes]:
    return {(prov,sy):(path/f"{prov}_{sy}.html").read_bytes()
            for prov in PROVIDERS for sy in SYMBOL_DATES}


def verify(*,root:Path|str,gap_report:Path|str,
           observations:Path|str=DEFAULT_OBSERVATIONS,
           history:Path|str=DEFAULT_HISTORY)->dict:
    path=Path(root).expanduser().resolve()
    manifest_bytes=(path/"manifest.json").read_bytes()
    if _sha(manifest_bytes)!=(path/"manifest.sha256").read_text().strip():
        raise ValueError("US public source manifest SHA changed")
    stored_manifest=json.loads(manifest_bytes)
    if stored_manifest.get("schema")!=VERSION:
        raise ValueError("Unsupported US public source schema")
    gap,gap_sha=_pinned_gap(Path(gap_report).resolve(),
                             Path(observations).resolve(),Path(history).resolve())
    raw=_load_raw(path)
    expected_manifest,expected_report=_derive(gap,gap_sha,raw,stored_manifest["captured_at_utc"])
    if stored_manifest!=expected_manifest:
        raise ValueError("US price source manifest does not match original source evidence")
    report_bytes=(path/"report.json").read_bytes()
    if _sha(report_bytes)!=(path/"report.sha256").read_text().strip():
        raise ValueError("US public price report SHA mismatch")
    if json.loads(report_bytes)!=expected_report:
        raise ValueError("US price comparison diverges from original source evidence")
    if path.stat().st_mode & 0o777 !=0o700:
        raise ValueError("Insecure source capture folder permissions")
    for filename in [*expected_manifest["raw_pages"],*FILES]:
        if (path/filename).stat().st_mode & 0o777 !=0o600:
            raise ValueError("Insecure US source evidence file permissions")
    return expected_report


def collect(*,root:Path|str,gap_report:Path|str,
            observations:Path|str=DEFAULT_OBSERVATIONS,
            history:Path|str=DEFAULT_HISTORY,
            getter=None,clock=None)->dict:
    path=Path(root).expanduser().resolve()
    if path.exists():
        raise ValueError("Immutable US public-source audit exists")
    gap,gap_sha=_pinned_gap(Path(gap_report).resolve(),
                             Path(observations).resolve(),Path(history).resolve())
    if getter is None:
        getter=requests.get
    if clock is None:
        clock=lambda:datetime.now(timezone.utc)
    timestamp=clock().astimezone(timezone.utc).isoformat()
    raw={}
    for sy in SYMBOL_DATES:
        for prov in PROVIDERS:
            url=SOURCE_URLS[(prov,sy)]
            # Fixed six public-website URLs; no credentials, no auth and no
            # redirect to an untrusted new domain.
            res=getter(url,headers=HEADERS,timeout=16,allow_redirects=False)
            if (res.status_code!=200 or len(res.content)>RAW_LIMIT_BYTES or
                len(res.content)<300):
                raise ValueError("Invalid third-party market history response")
            raw[(prov,sy)]=res.content
    manifest,report=_derive(gap,gap_sha,raw,timestamp)
    path.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".us-source-evidence-",dir=path.parent))
    try:
        stage.chmod(0o700)
        for (prov,sy),payload in raw.items():
            target=stage/f"{prov}_{sy}.html"
            target.write_bytes(payload)
            target.chmod(0o600)
        pairs={
            "report.json":(json.dumps(report,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode(),
            "manifest.json":(json.dumps(manifest,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        }
        pairs["report.sha256"]=(_sha(pairs["report.json"])+"\n").encode()
        pairs["manifest.sha256"]=(_sha(pairs["manifest.json"])+"\n").encode()
        for name,data in pairs.items():
            q=stage/name
            q.write_bytes(data)
            q.chmod(0o600)
        if path.exists():
            raise ValueError("Concurrent immutable US stock price audit exists")
        stage.rename(path)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    verify(root=path,gap_report=gap_report,observations=observations,history=history)
    return report


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="Research only: cross-check 5 US price gaps")
    p.add_argument("--gap-report",required=True)
    p.add_argument("--root",required=True)
    p.add_argument("--observations",default=DEFAULT_OBSERVATIONS)
    p.add_argument("--history",default=DEFAULT_HISTORY)
    p.add_argument("--verify-only",action="store_true")
    a=p.parse_args(argv)
    try:
        kw=dict(root=a.root,gap_report=a.gap_report,observations=a.observations,history=a.history)
        result=verify(**kw) if a.verify_only else collect(**kw)
        print(json.dumps({
            "status":result["status"],
            "stock_day_comparisons":result["checked_conflict_sessions"],
            "public_site_cents_agreements":result["public_sites_agree_at_displayed_cents"],
            "later_vendor_agreements":result["later_vendor_matches_both_displayed_cents"],
            "history_rows_modified":result["original_market_price_corrections_committed"],
            "orders":result["broker_orders"],
            "approved":result["backtest_paper_live_approved"],
        },sort_keys=True))
        return 0
    except (ValueError,KeyError,TypeError,OSError,requests.RequestException):
        print(json.dumps({"status":"US_PUBLIC_SOURCE_EVIDENCE_FAIL_CLOSED",
                          "orders":0,"approved":False}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

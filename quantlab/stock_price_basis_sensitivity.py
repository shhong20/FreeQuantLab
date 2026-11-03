"""Read-only price-basis sensitivity diagnostics with a QUODD third-source pin.

This module compares arithmetic OHLC-based diagnostics (NOT strategy returns)
for original, later-vendor, and issuer-hypothesis pricing. It re-verifies
the complete immutable upstream public-US report and ETF variant dataset from
original SHA-pinned raw stock evidence, then captures a third public provider's
three-decimal MSFT Oct 22 daily OHLC. Nothing changes historical bars/orders.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

import requests

from .stock_price_variant_dataset import verify as verify_etf
from .stock_us_public_price_audit import verify as verify_us, _PriceTable
from .stock_gap_provenance import DEFAULT_OBSERVATIONS,DEFAULT_HISTORY

VERSION="STOCK_PRICE_BASIS_SENSITIVITY_QUARANTINED_V1_20261103"
STATUS="OHLC_PRICE_BASIS_SENSITIVITY_ONLY_NO_STRATEGY_BACKTEST_OR_FILLS"
QUODD_URL="https://quodd.zacks.com/pricehistory/pricehistory.php?t=MSFT"
FILENAME="quodd_msft_20261022.html"
MAX_HTML_BYTES=150_000
MSFT_DAY="2026-10-22"
RAW_NOTE="third-party EOD price provider; NOT Nasdaq opening auction or exchange execution feed"

def _sha(data:bytes)->str:
    return sha256(data).hexdigest()


def _bps(x:Decimal,y:Decimal)->float:
    if y<=0:raise ValueError("Positive source price required")
    return float((x/y-Decimal(1))*Decimal(10_000))


def _to_prices(a:list)->list[Decimal]:
    if len(a)!=4 or any(type(v) not in (int,float,str,Decimal) for v in a):
        raise ValueError("Invalid price vector")
    x=[Decimal(str(v)) for v in a]
    if any(not y.is_finite() or y<=0 for y in x):
        raise ValueError("Nonpositive source price")
    if x[2]>min(x[0],x[3]) or x[1]<max(x[0],x[3]):
        raise ValueError("Invalid source OHLC range")
    return x


def _bar_metrics(ohlc:list[Decimal])->dict:
    o,h,l,c=ohlc
    return {
        "open_to_close_arithmetic_bps":_bps(c,o),
        "high_minus_low_relative_to_open_bps":float(
            ((h-l)/o)*Decimal(10_000)),
        "high_vs_open_bps":_bps(h,o),
        "low_vs_open_bps":_bps(l,o),
    }


def _parse_quodd(raw:bytes)->dict:
    if not raw or len(raw)>MAX_HTML_BYTES:
        raise ValueError("Unexpected third-party HTML size")
    text=raw.decode("utf-8")
    if "MSFT" not in text[:90_000]:
        raise ValueError("Unexpected third-party stock identity")
    p=_PriceTable()
    p.feed(text)
    tables=[t for t in p.tables if t and
            [v.lower() for v in t[0][:6]]==
            ["date","open","high","low","close","volume"]]
    if len(tables)!=1:
        raise ValueError("Missing or duplicated third-party history table")
    matches=[r for r in tables[0][1:] if r and r[0]=="10/22/2026"]
    if len(matches)!=1 or len(matches[0])!=6:
        raise ValueError("Missing or duplicated MSFT market-day row")
    row=matches[0]
    vals=[]
    for cell in row[1:5]:
        dec=Decimal(cell.replace(",",""))
        if not dec.is_finite() or dec<=0 or dec.as_tuple().exponent < -3:
            raise ValueError("Third provider OHLC precision invalid")
        vals.append(dec)
    _to_prices(vals)
    if Decimal(row[5].replace(",",""))<=0:
        raise ValueError("Third provider volume invalid")
    return {
        "source": "QUODD price history as published by Zacks",
        "symbol": "MSFT", "session": MSFT_DAY,
        "source_url":QUODD_URL,
        "original_display_precision":"USD 0.001 for this captured daily row",
        "ohlc_strings": [str(v) for v in vals],
        "volume_string":row[5].replace(",",""),
        "not_official_exchange_auction_reference":True,
    }


def _parents(*,us_root,etf_root,study_dir,gap_report,observations,history):
    us=verify_us(root=us_root,gap_report=gap_report,
                 observations=observations,history=history)
    etf=verify_etf(output_dir=etf_root,study_dir=study_dir,
                   gap_report=gap_report,observations=observations,history=history)
    if (us["broker_orders"]!=0 or us["backtest_paper_live_approved"] is not False or
        us["checked_conflict_sessions"]!=5 or us["original_market_price_corrections_committed"]!=0 or
        etf["broker_orders"]!=0 or etf["approved_for_backtest"] is not False or
        etf["etf_sessions"]!=4 or etf["us_unresolved_sessions"]!=5):
        raise ValueError("Upstream dataset not in research quarantine")
    return us,etf


def _diagnose(us:dict,etf_manifest:dict,etf_root:Path,quodd:dict)->dict:
    us_rows=[]
    for rec in sorted(us["comparisons"],key=lambda a:(a["symbol"],a["session"])):
        if (rec["price_gap_auto_resolved"] is not False or
            rec["model_training_backtest_paper_live_allowed"] is not False):
            raise ValueError("US public evidence wrongly declared as verified")
        orig=_to_prices(rec["original_observed_ohlc"])
        later=_to_prices(rec["latest_vendor_ohlc"])
        orig_m=_bar_metrics(orig)
        later_m=_bar_metrics(later)
        delta_price=[float(y-x) for x,y in zip(orig,later)]
        row={
            "market":"NASDAQ_NYSE","symbol":rec["symbol"],"session":rec["session"],
            "source_vendor_raw_sha256":rec["parent_vendor_raw_sha256"],
            "original_observed_ohlc":[float(x) for x in orig],
            "later_vendor_ohlc":[float(x) for x in later],
            "later_minus_original_usd_by_ohlc":delta_price,
            "abs_open_disagreement_bps_relative_original":abs(_bps(later[0],orig[0])),
            "original_bar_diagnostics":orig_m,
            "later_bar_diagnostics":later_m,
            "later_minus_original_open_close_move_bps":(
                later_m["open_to_close_arithmetic_bps"]-
                orig_m["open_to_close_arithmetic_bps"]),
            "later_minus_original_high_low_range_bps":(
                later_m["high_minus_low_relative_to_open_bps"]-
                orig_m["high_minus_low_relative_to_open_bps"]),
            "public_display_ohlc_2_sources_agree":
                rec["two_public_sites_same_displayed_cent_ohlc"],
            "independent_auction_trade_prices_certified":False,
            "historical_strategy_returns_certified":False,
            "canonical_history_price_selected":False,
            "eligible_for_model_or_trading":False,
        }
        if rec["symbol"]=="MSFT" and rec["session"]==MSFT_DAY:
            precise=_to_prices(quodd["ohlc_strings"])
            if (abs(precise[0]-later[0])>Decimal("0.0001") or
                abs(precise[1]-orig[1])>Decimal("0.0001") or
                any(abs(precise[i]-later[i])>Decimal("0.0001") for i in (2,3)) or
                not abs(precise[0]-orig[0])>Decimal("0.43")):
                raise ValueError("Third-party MSFT OHLC mixed-source evidence does not match")
            row["additional_third_party_price_precision_source"]={
                **quodd,
                "open_matches_later_vendor_to_0_0001_usd":True,
                "high_matches_initial_observed_to_0_0001_usd":True,
                "low_close_match_both_to_0_0001_usd":True,
                "high_differs_from_later_vendor_beyond_quodd_precision":True,
                "does_not_certify_original_open":True,
                "does_not_select_a_whole_candle_source_as_canonical":True,
            }
        us_rows.append(row)
    if len(us_rows)!=5:
        raise ValueError("Unexpected number of US comparison sessions")
    path=etf_root/"etf_variants.jsonl"
    e=[json.loads(s) for s in path.read_text().splitlines() if s.strip()]
    etf_rows=[]
    for rec in e:
        if (rec["canonical_trade_price_selected"] is not False or
            rec["eligible_for_backtesting_paper_live"] is not False):
            raise ValueError("ETF variant not quarantined")
        by={r["variant_id"]:_to_prices(r["ohlc_krw"]) for r in rec["variants"]}
        if set(by)!={"original_first_observed","latest_vendor_archived",
                    "issuer_cash_distribution_ratio_hypothesis"}:
            raise ValueError("Unexpected price candidates")
        original=by["original_first_observed"]
        factor=Decimal(str(rec["hypothesized_factor"]))
        expected_factor=(Decimal(95650)-Decimal(136))/Decimal(95650)
        hypothesized=by["issuer_cash_distribution_ratio_hypothesis"]
        if (rec["issuer_distribution_krw"]!=136 or
            abs(factor-expected_factor)>Decimal("0.000000000001") or
            rec["missing_from_primary_long_history"] is not True or
            any(abs(a*factor-b)>Decimal("0.00000001")
                for a,b in zip(original,hypothesized)) or
            any(abs(a-b)>Decimal("1.1")
                for a,b in zip(by["latest_vendor_archived"],hypothesized))):
            raise ValueError("ETF hypothesis not reproduced from immutable original")
        others={}
        for key,prices in sorted(by.items()):
            metrics=_bar_metrics(prices)
            others[key]={
                "open_level_difference_from_original_bps":_bps(prices[0],original[0]),
                "close_level_difference_from_original_bps":_bps(prices[3],original[3]),
                "bar_metrics":metrics,
                "open_close_arithmetic_move_difference_from_original_bps":(
                    metrics["open_to_close_arithmetic_bps"]-
                    _bar_metrics(original)["open_to_close_arithmetic_bps"]),
            }
        etf_rows.append({
            "market":"KRX","symbol":"069500","session":rec["session"],
            "source_vendor_raw_sha256":rec["source_vendor_raw_sha256"],
            "issuer_distribution_hypothesis_not_exchange_verified":True,
            "price_basis_scenarios":others,
            "cross_day_price_splicing_prohibited":True,
            "eligible_for_model_or_trading":False,
        })
    if len(etf_rows)!=4:
        raise ValueError("Expected four KODEX day scenarios")
    return {
        "schema_version":VERSION,
        "status":STATUS,
        "us_count":len(us_rows),
        "etf_count":len(etf_rows),
        "msft_2026_10_22_public_quodd_open":quodd["ohlc_strings"][0],
        "msft_open_first_observed_disagreement_usd":
            next(r for r in us_rows if r["symbol"]=="MSFT" and r["session"]==MSFT_DAY)[
                "later_minus_original_usd_by_ohlc"][0],
        "absolute_max_us_open_disagreement_bps":max(
            r["abs_open_disagreement_bps_relative_original"] for r in us_rows),
        "us_bar_sensitivity":us_rows,"etf_bar_sensitivity":etf_rows,
        "theoretical_price_movements_not_actual_strategy_returns":True,
        "no_filled_price_gaps":True,"backtest_or_live_approved":False,
        "strategy_pnl":None,"strategy_sharpe":None,
        "confirmed_market_order_fills":0,"broker_orders":0,
        "underlying_price_bases_unchanged":True,
        "official_nasdaq_opening_auction_trade_verified":False,
    }


def _manifest(report:dict,raw:bytes,parent_shas:dict)->dict:
    b=(json.dumps(report,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
    return {
        "schema_version":VERSION,"status":STATUS,
        "immutable_parent_sha256":parent_shas,
        "quodd_raw_html_sha256":_sha(raw),
        "quodd_raw_html_bytes":len(raw),
        "quodd_url":QUODD_URL,
        "source_quality":RAW_NOTE,
        "report_sha256":_sha(b),
        "broker_orders":0,"backtest_or_live_approved":False,
        "no_history_db_or_shadow_db_mutations":True,
    }


def _checksums(*,us_root:Path,etf_root:Path,study_dir:Path,gap_report:Path)->dict:
    return {
        "us_public_report":_sha((us_root/"report.json").read_bytes()),
        "us_public_manifest":_sha((us_root/"manifest.json").read_bytes()),
        "etf_variant_manifest":_sha((etf_root/"manifest.json").read_bytes()),
        "etf_study":_sha((study_dir/"study.json").read_bytes()),
        "gap_report":_sha(gap_report.read_bytes()),
    }


def _context(**kwargs):
    paths={k:Path(kwargs[k]).expanduser().resolve() for k in (
        "us_root","etf_root","study_dir","gap_report","observations","history")}
    us,etf=_parents(**paths)
    return paths,us,etf


def verify(*,root,us_root,etf_root,study_dir,gap_report,
           observations=DEFAULT_OBSERVATIONS,history=DEFAULT_HISTORY):
    paths,us,etf=_context(us_root=us_root,etf_root=etf_root,study_dir=study_dir,
                          gap_report=gap_report,observations=observations,
                          history=history)
    folder=Path(root).expanduser().resolve()
    payload=(folder/"report.json").read_bytes()
    manifest_raw=(folder/"manifest.json").read_bytes()
    if (_sha(payload)!=(folder/"report.sha256").read_text().strip() or
        _sha(manifest_raw)!=(folder/"manifest.sha256").read_text().strip()):
        raise ValueError("Source sensitivity report SHA mismatch")
    raw=(folder/FILENAME).read_bytes()
    quodd=_parse_quodd(raw)
    independent=_diagnose(us,etf,paths["etf_root"],quodd)
    if json.loads(payload)!=independent:
        raise ValueError("Source sensitivity report changed from immutable parents")
    expected_manifest=_manifest(independent,raw,_checksums(**{
        k:paths[k] for k in ("us_root","etf_root","study_dir","gap_report")}))
    if json.loads(manifest_raw)!=expected_manifest:
        raise ValueError("Source sensitivity manifest changed")
    if folder.stat().st_mode&0o777!=0o700:
        raise ValueError("Insecure research directory")
    for f in (FILENAME,"report.json","report.sha256","manifest.json","manifest.sha256"):
        if (folder/f).stat().st_mode&0o777!=0o600:
            raise ValueError("Insecure research artifact")
    return independent


def collect(*,root,us_root,etf_root,study_dir,gap_report,
            observations=DEFAULT_OBSERVATIONS,history=DEFAULT_HISTORY,
            getter=None):
    folder=Path(root).expanduser().resolve()
    if folder.exists():
        raise ValueError("Immutable research report already exists")
    paths,us,etf=_context(us_root=us_root,etf_root=etf_root,study_dir=study_dir,
                          gap_report=gap_report,observations=observations,
                          history=history)
    if getter is None:
        getter=requests.get
    response=getter(QUODD_URL,timeout=15,allow_redirects=False,headers={
        "User-Agent":"Mozilla/5.0 (compatible; ResearchValidation/1.0)"})
    raw=response.content
    if response.status_code!=200 or len(raw)>MAX_HTML_BYTES:
        raise ValueError("Third source price history HTTP failed")
    quodd=_parse_quodd(raw)
    report=_diagnose(us,etf,paths["etf_root"],quodd)
    manifest=_manifest(report,raw,_checksums(**{
        k:paths[k] for k in ("us_root","etf_root","study_dir","gap_report")}))
    folder.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".price-sensitivity-",dir=folder.parent))
    try:
        stage.chmod(0o700)
        contents={
            FILENAME:raw,
            "report.json":(json.dumps(report,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode(),
            "manifest.json":(json.dumps(manifest,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode(),
        }
        contents["report.sha256"]=(_sha(contents["report.json"])+"\n").encode()
        contents["manifest.sha256"]=(_sha(contents["manifest.json"])+"\n").encode()
        for name,blob in contents.items():
            f=stage/name
            f.write_bytes(blob)
            f.chmod(0o600)
        if folder.exists():
            raise ValueError("Concurrent sensitivity research output already exists")
        stage.rename(folder)
    finally:
        if stage.exists():shutil.rmtree(stage)
    verify(root=folder,**paths)
    return report


def main(argv:list[str]|None=None)->int:
    ap=argparse.ArgumentParser(description="Arithmetical OHLC-only price basis sensitivity")
    for name in ("root","us-root","etf-root","study-dir","gap-report"):
        ap.add_argument("--"+name,required=True)
    ap.add_argument("--observations",default=DEFAULT_OBSERVATIONS)
    ap.add_argument("--history",default=DEFAULT_HISTORY)
    ap.add_argument("--verify-only",action="store_true")
    args=ap.parse_args(argv)
    kw={
        "root":args.root,"us_root":args.us_root,"etf_root":args.etf_root,
        "study_dir":args.study_dir,"gap_report":args.gap_report,
        "observations":args.observations,"history":args.history,
    }
    try:
        data=verify(**kw) if args.verify_only else collect(**kw)
        print(json.dumps({
            "status":data["status"],
            "msft_open_difference_usd":data["msft_open_first_observed_disagreement_usd"],
            "max_us_open_basis_difference_bps":data["absolute_max_us_open_disagreement_bps"],
            "us_sessions":data["us_count"],"etf_sessions":data["etf_count"],
            "strategy_pnl":None,"broker_orders":0,"trading_approved":False
        },sort_keys=True))
        return 0
    except (ValueError,KeyError,TypeError,OSError,requests.RequestException):
        print(json.dumps({"status":"SOURCE_SENSITIVITY_FAIL_CLOSED",
                          "strategy_pnl":None,"broker_orders":0,"trading_approved":False}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

"""Stock data quality and forward Qlib *eligibility* audit only.

Reads independent immutable public daily OHLCV archive; compares actual market
sessions to versioned reference calendars without synthesizing holidays.
Extracts THIRD-PARTY Yahoo dividend/split/adjclose event evidence already
preserved as raw SHA256 snapshots; never accepts the presence of these
events as issuer-certified corporate actions or as total-return cash flows.
A 90-day stock Qlib lane is deliberately NOT created until upstream data,
frozen model, total return and market-calendar evidence are all verified.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from uuid import uuid4
import sqlite3

from .stock_daily_observer import DEFAULT_DB, PROVIDER, UNIVERSE

# Exchange calendar packages may not incorporate last-minute Korean holidays.
# This exception is public but we do NOT certify a full official KRX calendar.
KNOWN_2026_KRX_REFERENCE_EXCEPTIONS={
    "2026-06-03": "KRX securities market closure on 2026 local-election day; library may lag",
    "2026-07-17": "2026 Constitution Day market-closure notice; exchange calendar library may lag",
}
REQUIRED_QLIB_DAILY_SESSIONS=662  # existing cross_market START_TRAIN=600+60+2


def _normal_date(value:str)->date:
    return date.fromisoformat(value)


def _reference_sessions(market:str,start:str,end:str,calendar_getter=None)->dict:
    calname="XKRX" if market=="KRX" else "XNYS"
    if calendar_getter is None:
        try:
            import exchange_calendars as ec
            calendar=ec.get_calendar(calname)
            version="exchange_calendars_"+ec.__version__
        except (ImportError,KeyError,ValueError,OverflowError) as exc:
            return {"status":"REFERENCE_CALENDAR_NOT_AVAILABLE",
                    "name":calname,"version":type(exc).__name__,
                    "sessions":[]}
    else:
        calendar=calendar_getter(calname)
        version="EXPLICIT_TEST_REFERENCE"
    sessions=calendar.sessions_in_range(start,end)
    return {"status":"REFERENCE_CALENDAR_NOT_OFFICIAL",
            "name":calname,"version":version,
            "sessions":[x.date().isoformat() for x in sessions]}


def _provider_corporate_events(market:str,ticker:str,raw:bytes,
                               first:str,last:str)->dict:
    if market!="NASDAQ_NYSE":
        return {"provider":"naver_unofficial_siseJson",
                "vendor_cash_dividend_events":[],"vendor_stock_split_events":[],
                "adjusted_close_points":0,
                "issuer_verified_distribution_history":False,
                "corporate_actions_verified":False,
                "adjustment_factor_verified":False,
                "status":"KRX_ISSUER_EVENT_HISTORY_NOT_VERIFIED"}
    outer=json.loads(raw).get("chart",{}).get("result")
    if not isinstance(outer,list) or len(outer)!=1:
        raise ValueError("Archived Yahoo source does not contain a single chart")
    doc=outer[0]
    if (doc.get("meta",{}).get("symbol")!=ticker
        or doc["meta"].get("currency")!="USD"
        or doc["meta"].get("exchangeTimezoneName")!="America/New_York"):
        raise ValueError("Archived Yahoo metadata differs from registered stock")
    events=doc.get("events") or {}
    if not isinstance(events,dict) or not set(events).issubset({"dividends","splits","capitalGains"}):
        raise ValueError("Unsupported vendor corporate action event schema")
    collected={}
    for kind in ("dividends","splits"):
        entries=events.get(kind,{})
        if not isinstance(entries,dict) or len(entries)>250:
            raise ValueError("Invalid vendor event dictionary")
        items=[]
        for label,value in entries.items():
            if not isinstance(value,dict) or type(value.get("date")) is not int:
                raise ValueError("Invalid vendor event timestamp")
            stamp=datetime.fromtimestamp(value["date"],timezone.utc).date().isoformat()
            record={"vendor_event_utc_day":stamp,
                    "event_data_sha256":sha256(json.dumps(
                        value,sort_keys=True,separators=(",",":")
                    ).encode()).hexdigest()}
            if kind=="dividends":
                amount=value.get("amount")
                if type(amount) not in (int,float) or not math.isfinite(amount) or amount<=0:
                    raise ValueError("Invalid vendor cash dividend amount")
                record["vendor_cash_amount_usd_per_share"]=float(amount)
            else:
                numerator=value.get("numerator")
                denominator=value.get("denominator")
                if (type(numerator) not in (int,float) or
                    type(denominator) not in (int,float) or
                    numerator<=0 or denominator<=0):
                    raise ValueError("Invalid vendor split factor")
                record["vendor_split_ratio"]=[numerator,denominator]
            items.append(record)
        collected[kind]=sorted(items,key=lambda x:x["vendor_event_utc_day"])
    quote=doc.get("indicators",{}).get("quote")
    adj=doc.get("indicators",{}).get("adjclose")
    size=len(doc.get("timestamp",[]))
    adjusted_points=0
    if isinstance(adj,list) and len(adj)==1 and isinstance(adj[0],dict):
        series=adj[0].get("adjclose")
        if not isinstance(series,list) or len(series)!=size:
            raise ValueError("Vendor adjusted close vector length mismatch")
        adjusted_points=sum(type(x) in (int,float) and math.isfinite(x) and x>0
                            for x in series)
    return {
        "provider":"yahoo_unofficial_chart",
        "vendor_cash_dividend_events":collected["dividends"],
        "vendor_stock_split_events":collected["splits"],
        "adjusted_close_points":adjusted_points,
        "vendor_adjclose_not_audited_against_issuer":True,
        "issuer_verified_distribution_history":False,
        "corporate_actions_verified":False,
        "adjustment_factor_verified":False,
        "status":"VENDOR_EVENTS_ONLY_NOT_OFFICIAL_CORPORATE_ACTIONS"
    }


def inspect_stock_evidence(db:str|Path=DEFAULT_DB,*,calendar_getter=None)->dict:
    path=Path(db).resolve()
    if path.name!="observations.sqlite3" or path.parent.name!="stock_daily":
        raise ValueError("Stock evidence must use isolated stock_daily SQLite")
    if not path.is_file():
        raise FileNotFoundError("Separate stock daily ledger is missing")
    result={"status":"BLOCKED_STOCK_TOTAL_RETURN_AND_QLIB_FORWARD_REGISTRATION",
            "observed_at_utc":datetime.now(timezone.utc).isoformat(),
            "markets":{},"unresolved_issues":[],
            "minimum_real_daily_sessions_for_existing_qlib":REQUIRED_QLIB_DAILY_SESSIONS,
            "90_day_forward_qlib_registered":False,
            "preexisting_crypto_cohort_unchanged":True,
            "official_KRX_and_US_session_calendars_attested":False,
            "issuer_cash_distribution_and_corporate_actions_attested":False,
            "total_return_performance_publishable":False,
            "automatic_strategy_promotion":False,
            "stock_paper_orders_placed":0,
            "real_orders_enabled":False}
    with sqlite3.connect("file:"+str(path)+"?mode=ro",uri=True,timeout=15) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("Stock evidence SQLite quick_check failed")
        # Fail closed on ANY previous source revision or fetch failure.
        total_revisions=conn.execute("SELECT COUNT(*) FROM revision_alerts").fetchone()[0]
        total_failures=conn.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0]
        for market,tickers in UNIVERSE.items():
            result["markets"][market]={}
            for ticker in tickers:
                dates=[d[0] for d in conn.execute(
                    """SELECT session_date FROM daily_bars
                       WHERE source=? AND market=? AND ticker=? ORDER BY session_date""",
                    (PROVIDER[market],market,ticker))]
                if not dates:
                    raise ValueError("Stock missing minimum captured history: "+ticker)
                if len(dates)!=len(set(dates)) or any(
                    _normal_date(d).weekday()>=5 for d in dates):
                    raise ValueError("Unexpected weekend or duplicate stock session")
                snapshot=conn.execute(
                    """SELECT raw,payload_sha256 FROM source_snapshots
                       WHERE source=? AND market=? AND ticker=? ORDER BY id DESC LIMIT 1""",
                    (PROVIDER[market],market,ticker)).fetchone()
                if not snapshot or sha256(snapshot[0]).hexdigest()!=snapshot[1]:
                    raise ValueError("Archived stock source response SHA256 differs")
                ref=_reference_sessions(market,dates[0],dates[-1],calendar_getter)
                reference=set(ref["sessions"])
                observed=set(dates)
                expected_missing=sorted(reference-observed)
                unexpected=sorted(observed-reference)
                closure=[{"session":x,
                          "reason":KNOWN_2026_KRX_REFERENCE_EXCEPTIONS[x]}
                         for x in expected_missing
                         if market=="KRX" and x in KNOWN_2026_KRX_REFERENCE_EXCEPTIONS]
                unknown_missing=[x for x in expected_missing
                                 if not (market=="KRX" and
                                         x in KNOWN_2026_KRX_REFERENCE_EXCEPTIONS)]
                official_attested=False
                corp=_provider_corporate_events(market,ticker,snapshot[0],
                                                dates[0],dates[-1])
                reasons=[]
                if len(dates)<REQUIRED_QLIB_DAILY_SESSIONS:
                    reasons.append("QLIB_MINIMUM_HISTORICAL_TRAIN_SESSIONS_NOT_MET")
                if ref["status"]!="REFERENCE_CALENDAR_NOT_OFFICIAL":
                    reasons.append("REFERENCE_CALENDAR_UNAVAILABLE")
                elif unknown_missing or unexpected:
                    reasons.append("REFERENCE_SESSION_DISAGREEMENT_NEEDS_REVIEW")
                if closure:
                    reasons.append("KNOWN_REFERENCE_LIBRARY_HOLIDAY_MISMATCH")
                reasons.extend([
                    "OFFICIAL_EXCHANGE_SESSION_CALENDAR_NOT_ATTESTED",
                    "ISSUER_DIVIDEND_SPLIT_OR_ETF_DISTRIBUTION_HISTORY_NOT_ATTESTED",
                    "ADJUSTED_TOTAL_RETURN_SERIES_NOT_VERIFIED",
                    "STOCK_Qlib_FROZEN_MODEL_NOT_REGISTERED",
                ])
                result["markets"][market][ticker]={
                    "observed_sessions":len(dates),"first":dates[0],"last":dates[-1],
                    "latest_snapshot_sha256":snapshot[1],
                    "reference_calendar":ref["name"],
                    "calendar_library_version":ref["version"],
                    "reference_expected_sessions":len(reference),
                    "reference_expected_but_missing":expected_missing,
                    "reference_unexpected_vendor_dates":unexpected,
                    "known_reference_calendar_closure_disagreements":closure,
                    "unexplained_reference_gaps":unknown_missing,
                    "official_exchange_calendar_attested":official_attested,
                    "corporate_action_vendor_evidence":corp,
                    "eligible_for_qlib_forward_paper":False,
                    "blocks":reasons,
                }
    result["historical_vendor_price_revision_alerts"]=total_revisions
    result["historical_vendor_fetch_failures"]=total_failures
    result["unresolved_issues"]=[
        "NO_SIGNED_OFFICIAL_MARKET_SESSION_SOURCE",
        "NO_ISSUER_VERIFIED_FULL_DIVIDEND_SPLIT_AND_ETF_DISTRIBUTION_LEDGER",
        "NO_CAUSAL_662_SESSION_STOCK_MODEL_HISTORY",
        "NO_FROZEN_OOS_STOCK_QLIB_MODEL",
    ]
    if total_revisions:
        result["unresolved_issues"].append("IMMUTABLE_VENDOR_REVISIONS_TO_RECONCILE")
    if total_failures:
        result["unresolved_issues"].append("PROVIDER_FETCH_FAILURES_TO_REVIEW")
    # Long history is separate and immutable; never allow 3-month daily
    # observation coverage to conceal its presence OR claim that enough
    # bars alone makes the data approved for live/paper Qlib.
    long_path=path.with_name("history.sqlite3")
    if long_path.exists():
        from .stock_long_history import audit_history
        evidence=audit_history(long_path)
        result["independent_long_history"]={
            "status":evidence["status"],
            "all_six_662_session_price_lengths_met":
                evidence["all_6_assets_have_sufficient_price_history_length"],
            "all_raw_payload_SHA256_verified":
                evidence["sha256_all_history_source_payloads_verified"],
            "material_conflicts":evidence["material_price_or_adjustment_conflicts"],
            "vendor_adjustment_rounding_jitter_kept":
                evidence["retained_vendor_adjustment_precision_jitter_events"],
            "official_dividend_split_and_ETF_distribution_not_verified":True,
        }
        for market,tickers in UNIVERSE.items():
            for ticker in tickers:
                current=result["markets"][market][ticker]
                history=evidence["markets"][market][ticker]
                current["independent_long_vendor_sessions"]=history[
                    "completed_vendor_daily_sessions"]
                current["long_vendor_history_length_662_met"]=history[
                    "historical_length_threshold_662_met"]
                current["long_history_reference_gaps_not_explained"]=history[
                    "unknown_reference_calendar_missing_sessions"]
                current["long_history_known_2026_KRX_closures"]=history[
                    "known_exchange_closure_missing_in_library"]
                if history["historical_length_threshold_662_met"]:
                    if "QLIB_MINIMUM_HISTORICAL_TRAIN_SESSIONS_NOT_MET" in current["blocks"]:
                        current["blocks"].remove(
                            "QLIB_MINIMUM_HISTORICAL_TRAIN_SESSIONS_NOT_MET")
                    current["blocks"].append(
                        "LONG_PRICE_HISTORY_AVAILABLE_BUT_UNVERIFIED_FOR_TOTAL_RETURN")
        if evidence["all_6_assets_have_sufficient_price_history_length"]:
            result["unresolved_issues"].remove(
                "NO_CAUSAL_662_SESSION_STOCK_MODEL_HISTORY")
            result["unresolved_issues"].append(
                "LONG_VENDOR_PRICE_HISTORY_EXISTS_BUT_NO_CAUSAL_FROZEN_STOCK_QLIB_MODEL")
        if evidence["material_price_or_adjustment_conflicts"]:
            result["unresolved_issues"].append(
                "LONG_VENDOR_PRICE_OR_ADJUSTMENT_CONFLICTS_REQUIRE_REVIEW")
        if not evidence["sha256_all_history_source_payloads_verified"]:
            raise ValueError("Long stock evidence raw SHA256 integrity failure")
    return result


def save_quality_receipt(report:dict,root:str|Path)->dict:
    """Append-only local stock-only research status receipt; never a trading approval."""
    target=Path(root).absolute()
    if target.name!="quality_reports" or target.parent.name!="stock_daily":
        raise ValueError("Refuse saving market audit outside isolated stock_daily folder")
    if target.is_symlink() or not target.parent.is_dir():
        raise ValueError("Stock-only audit receipt location absent or unsafe")
    target.mkdir(mode=0o700,exist_ok=True)
    if target.is_symlink():
        raise ValueError("Symlink quality report directory")
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename=stamp+"-"+uuid4().hex[:12]+".json"
    raw=(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
    path=target/filename
    with path.open("xb") as fp:
        os.fchmod(fp.fileno(),0o600)
        fp.write(raw)
        fp.flush()
        os.fsync(fp.fileno())
    return {"receipt_path":str(path),"receipt_sha256":sha256(raw).hexdigest(),
            "receipt_is_not_issuer_or_exchange_signed":True}


def main(argv=None):
    p=argparse.ArgumentParser(description="Read-only stock calendar/vendor-event and Qlib readiness audit, no trading")
    p.add_argument("--db",default=DEFAULT_DB)
    p.add_argument("--receipt-dir",default=None,
                   help="Optional append-only research audit receipts within stock_daily/quality_reports")
    p.add_argument("--strict-eligible",action="store_true",
                   help="Exit code 2 while total-return and live Qlib registration remain blocked")
    args=p.parse_args(argv)
    doc=inspect_stock_evidence(args.db)
    if args.receipt_dir:
        doc["receipt"]=save_quality_receipt(doc,args.receipt_dir)
    print(json.dumps(doc,ensure_ascii=False,sort_keys=True))
    # In a scheduled audit 'blocked for trading' is expected, not an
    # infrastructure failure. Use --strict-eligible for release gates.
    return 2 if args.strict_eligible else 0


if __name__=="__main__":
    raise SystemExit(main())

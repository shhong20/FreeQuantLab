"""Fail-closed Korean ETF provenance, calendar and distribution-readiness audit.

A price-only Naver OHLCV CSV, even if identical to two other ETFs on all
trade dates, is NOT evidence of adjusted-price, distribution, split, fund
tracking, or a tradable total-return stream. Do not derive payout events from
negative returns or silently fill exchange closures.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from .qlib_cross_market import GROUPS, validate_market

# These two dates are publicly described as Korean closure holidays in 2026.
# An installed market-calendar library can be stale; disagreements MUST be
# reported, not treated as missing bars or auto-created trading sessions.
KNOWN_2026_HOLIDAYS={"2026-06-03":"local_elections",
                     "2026-07-17":"constitution_day_restored"}


def audit_etf_snapshot(snapshot_dir: str | Path, calendar=None) -> dict:
    root=Path(snapshot_dir)
    manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    family="krx_local_sessions"
    cfg=GROUPS[family]
    evidence=manifest["groups"][family]
    ids=(cfg["source"],*cfg["targets"])
    if evidence["source"]!=ids[0] or evidence["target_ids"]!=list(ids[1:]):
        raise ValueError("ETF universe or training source not registered")
    data={}
    rows={}
    for sym in ids:
        info=evidence["assets"][sym]
        name=f"{family}_{sym}.csv"
        if name!=info["filename"]:
            raise ValueError("Source filename tampering")
        raw=(root/name).read_bytes()
        if sha256(raw).hexdigest()!=info["sha256"]:
            raise ValueError("Archived ETF OHLCV SHA256 mismatch")
        frame=validate_market(pd.read_csv(root/name),"naver")
        if (len(frame)!=info["records"]
            or frame["date"].iloc[0].date().isoformat()!=info["first"]
            or frame["date"].iloc[-1].date().isoformat()!=info["last"]):
            raise ValueError("ETF archive metadata not consistent")
        data[sym]=frame
        rows[sym]={"source_sha256":info["sha256"],"n_sessions":len(frame),
                   "first":info["first"],"last":info["last"],
                   "price_adjustments_verified":False,
                   "issuer_cash_distributions_verified":False,
                   "corporate_action_history_verified":False,
                   "fund_total_return_ready":False,
                   "issues":["NO_AUDITED_ISSUER_TOTAL_RETURN_PRICE",
                             "DISTRIBUTION_EVENTS_UNVERIFIED",
                             "CORPORATE_ACTIONS_UNVERIFIED"]}
    common=data[ids[0]]["date"]
    if any(not common.equals(data[sym]["date"]) for sym in ids[1:]):
        raise ValueError("ETF target calendars differ; missing sessions cannot be interpolated")

    # Calendar comparison is *supplemental*: packages may lag 2026 holiday
    # changes and cannot alone certify actual KRX open trading sessions.
    version="NOT_INSTALLED"
    missing=[]
    noncalendar=[]
    if calendar is None:
        try:
            import exchange_calendars
            version=f"exchange_calendars_{exchange_calendars.__version__}"
            calendar=exchange_calendars.get_calendar("XKRX")
        except (ImportError, KeyError, ValueError):
            version="NO_CALENDAR_LIBRARY"
    if calendar is not None:
        cal_sessions=pd.DatetimeIndex(calendar.sessions_in_range(common.iloc[0],common.iloc[-1])).tz_localize(None)
        actual=pd.DatetimeIndex(common).tz_localize(None)
        missing=[x.date().isoformat() for x in cal_sessions.difference(actual)]
        noncalendar=[x.date().isoformat() for x in actual.difference(cal_sessions)]
    known_closures=[{"date":day,"reason":KNOWN_2026_HOLIDAYS[day]}
                    for day in missing if day in KNOWN_2026_HOLIDAYS]
    unexplained=[day for day in missing if day not in KNOWN_2026_HOLIDAYS]
    return {
        "status":"BLOCKED_TOTAL_RETURN_VERIFICATION",
        "source":"naver_unofficial_historical_OHLCV",
        "funds":rows,
        "shared_trading_sessions":len(common),
        "all_etf_market_session_dates_identical":True,
        "calendar_reference":version,
        "external_calendar_missing_sessions":missing,
        "external_calendar_noncalendar_sessions":noncalendar,
        "known_2026_public_holidays_missed_by_external_calendar":known_closures,
        "unexplained_calendar_expected_but_missing":unexplained,
        "official_KRX_calendar_independently_verified":False,
        "security_master_ticker_history_verified":False,
        "cash_distributions_accrued":False,
        "price_adjusted_for_corporate_actions":False,
        "total_return_performance_publishable":False,
        "real_orders_enabled":False,
        "automatic_strategy_promotion":False,
        "manual_evidence_required":[
            "Official KRX session and suspension/holiday data, including 2026 changes",
            "Issuer-vetted ex-dividend, record, payment dates and cash per share by ETF",
            "Unadjusted executable market OHLCV and splits/corporate actions",
            "Adjusted NAV/total-return cross-check and withholding/cash accrual rules",
            "Daily inverse and gold-linked fund benchmark/tracking/basis definitions",
        ],
    }

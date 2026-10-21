"""Read-only vendor-event/corporate-action and reference-session Qlib gates."""
from __future__ import annotations

from datetime import datetime,timezone
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from quantlab import stock_daily_observer as source
from quantlab import stock_evidence_quality as q
from test_stock_daily_observer import NOW,naver,yahoo,Response,us_payload


class Calendar:
    def __init__(self,add_exception=False):
        self.add_exception=add_exception
    def sessions_in_range(self,start,end):
        dates=list(pd.date_range(start,end,freq="B"))
        if self.add_exception:
            dates=[*dates,pd.Timestamp("2026-07-17")]
        return pd.DatetimeIndex(sorted(set(dates)))


def calendar(name):
    assert name in ("XKRX","XNYS")
    return Calendar()


def seed(tmp_path,*,us_getter=yahoo):
    db=tmp_path/"stock_daily"/"observations.sqlite3"
    result=source.capture(db,now=NOW,naver_getter=naver,yahoo_getter=us_getter)
    assert result["status"]=="STOCK_PUBLIC_DAILY_DATA_CAPTURED_NO_TRADING"
    return db


def test_readiness_flags_all_six_assets_and_blocks_stock_qlib_forward(tmp_path):
    db=seed(tmp_path)
    r=q.inspect_stock_evidence(db,calendar_getter=calendar)
    assert r["status"]=="BLOCKED_STOCK_TOTAL_RETURN_AND_QLIB_FORWARD_REGISTRATION"
    assert r["minimum_real_daily_sessions_for_existing_qlib"]==662
    assert set(r["markets"]["KRX"])==set(source.KR)
    assert set(r["markets"]["NASDAQ_NYSE"])==set(source.US)
    assert all(not v["eligible_for_qlib_forward_paper"]
               for x in r["markets"].values() for v in x.values())
    assert not r["90_day_forward_qlib_registered"]
    assert not r["total_return_performance_publishable"]
    assert r["stock_paper_orders_placed"]==0
    assert r["real_orders_enabled"] is False
    assert "NO_FROZEN_OOS_STOCK_QLIB_MODEL" in r["unresolved_issues"]


def test_known_krx_holiday_in_reference_calendar_not_fake_filled(tmp_path):
    db=seed(tmp_path)
    with sqlite3.connect(db) as c:
        for ticker in source.KR:
            s=source.PROVIDER["KRX"]
            c.execute("""UPDATE daily_bars SET session_date='2026-07-16'
                      WHERE source=? AND market='KRX' AND ticker=? AND session_date='2026-10-19'""",(s,ticker))
            c.execute("""UPDATE daily_bars SET session_date='2026-07-20'
                      WHERE source=? AND market='KRX' AND ticker=? AND session_date='2026-10-20'""",(s,ticker))
            c.execute("""UPDATE daily_bars SET session_date='2026-07-21'
                      WHERE source=? AND market='KRX' AND ticker=? AND session_date='2026-10-21'""",(s,ticker))
    def get_calendar(name):
        if name=="XKRX":
            class Reference:
                def sessions_in_range(self,start,end):
                    return pd.DatetimeIndex([pd.Timestamp("2026-07-16"),
                                             pd.Timestamp("2026-07-17"),
                                             pd.Timestamp("2026-07-20"),
                                             pd.Timestamp("2026-07-21")])
            return Reference()
        return Calendar()
    result=q.inspect_stock_evidence(db,calendar_getter=get_calendar)
    sample=result["markets"]["KRX"]["005930"]
    assert sample["reference_expected_but_missing"]==["2026-07-17"]
    assert sample["unexplained_reference_gaps"]==[]
    assert sample["known_reference_calendar_closure_disagreements"][0]["session"]=="2026-07-17"
    assert sample["observed_sessions"]==3
    assert "KNOWN_REFERENCE_LIBRARY_HOLIDAY_MISMATCH" in sample["blocks"]


def test_us_dividend_and_adjclose_snapshot_is_just_vendor_evidence(tmp_path):
    def us_with_event(url,**kwargs):
        t=url.rsplit("/",1)[-1]
        obj=json.loads(us_payload(t))
        chart=obj["chart"]["result"][0]
        chart["events"]={"dividends":{"a":{
            "amount":0.27,"date":1792488600
        }},"splits":{}}
        chart["indicators"]["adjclose"]=[{"adjclose":[98.,100.,105.]}]
        return Response(json.dumps(obj).encode(),url)
    db=seed(tmp_path,us_getter=us_with_event)
    report=q.inspect_stock_evidence(db,calendar_getter=calendar)
    for tick in source.US:
        ev=report["markets"]["NASDAQ_NYSE"][tick]["corporate_action_vendor_evidence"]
        assert len(ev["vendor_cash_dividend_events"])==1
        assert ev["adjusted_close_points"]==3
        assert ev["vendor_cash_dividend_events"][0]["vendor_cash_amount_usd_per_share"]==0.27
        assert ev["status"]=="VENDOR_EVENTS_ONLY_NOT_OFFICIAL_CORPORATE_ACTIONS"
        assert not ev["issuer_verified_distribution_history"]
        assert not ev["corporate_actions_verified"]
    assert not report["total_return_performance_publishable"]


def test_no_vendor_events_is_not_evidence_of_no_distributions(tmp_path):
    db=seed(tmp_path)
    r=q.inspect_stock_evidence(db,calendar_getter=calendar)
    us=r["markets"]["NASDAQ_NYSE"]["AAPL"]["corporate_action_vendor_evidence"]
    assert us["vendor_cash_dividend_events"]==[]
    assert us["issuer_verified_distribution_history"] is False
    kr=r["markets"]["KRX"]["069500"]["corporate_action_vendor_evidence"]
    assert kr["status"]=="KRX_ISSUER_EVENT_HISTORY_NOT_VERIFIED"


def test_corrupt_snapshot_denied_before_registration(tmp_path):
    db=seed(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE source_snapshots SET raw=? WHERE id=1",(b"broken",))
    with pytest.raises(ValueError,match="SHA256"):
        q.inspect_stock_evidence(db,calendar_getter=calendar)


def test_missing_stock_source_and_crypto_ledger_not_accepted(tmp_path):
    db=tmp_path/"stock_daily"/"observations.sqlite3"
    with pytest.raises(FileNotFoundError):
        q.inspect_stock_evidence(db,calendar_getter=calendar)
    db=tmp_path/"shadow.db"
    db.write_bytes(b"not sqlite")
    with pytest.raises(ValueError,match="isolated"):
        q.inspect_stock_evidence(db,calendar_getter=calendar)


def test_out_of_range_vendor_cash_event_refused(tmp_path):
    def wrong(url,**kwargs):
        obj=json.loads(us_payload(url.rsplit("/",1)[-1]))
        obj["chart"]["result"][0]["events"]={"dividends":{"a":{
            "amount":-4,"date":1792488600
        }}}
        return Response(json.dumps(obj).encode(),url)
    db=seed(tmp_path,us_getter=wrong)
    with pytest.raises(ValueError,match="Invalid vendor cash dividend"):
        q.inspect_stock_evidence(db,calendar_getter=calendar)


def test_append_only_quality_receipts_are_valid_JSON_and_never_trading_approval(tmp_path):
    db=seed(tmp_path)
    report=q.inspect_stock_evidence(db,calendar_getter=calendar)
    target=tmp_path/"stock_daily"/"quality_reports"
    a=q.save_quality_receipt(report,target)
    b=q.save_quality_receipt(report,target)
    assert a["receipt_path"]!=b["receipt_path"]
    for item in (a,b):
        payload=Path(item["receipt_path"]).read_bytes()
        assert json.loads(payload)["status"]=="BLOCKED_STOCK_TOTAL_RETURN_AND_QLIB_FORWARD_REGISTRATION"
        assert q.sha256(payload).hexdigest()==item["receipt_sha256"]
    assert len(list(target.glob("*.json")))==2
    assert not report["90_day_forward_qlib_registered"]


def test_evidence_receipts_reject_crypto_folder_or_symlink(tmp_path):
    db=seed(tmp_path)
    report=q.inspect_stock_evidence(db,calendar_getter=calendar)
    with pytest.raises(ValueError,match="isolated"):
        q.save_quality_receipt(report,tmp_path/"crypto"/"quality_reports")
    elsewhere=tmp_path/"elsewhere"
    elsewhere.mkdir()
    link=tmp_path/"stock_daily"/"quality_reports"
    link.symlink_to(elsewhere,target_is_directory=True)
    with pytest.raises(ValueError,match="absent or unsafe"):
        q.save_quality_receipt(report,link)

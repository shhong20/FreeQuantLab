"""2026-10-29 ETF distribution hypothesis vs SHA-backed missing stock sessions.

Never writes source histories, never authorizes historical fills, and never claims
that an issuer cash distribution proves a vendor's proprietary adjustment method.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

from .stock_gap_provenance import (
    DEFAULT_OBSERVATIONS, DEFAULT_HISTORY, inspect,
)

ISSUER="삼성자산운용"
TICKER="069500"
PUBLISHED_UTC_DAY="2026-10-27"
DISTRIBUTION_KRW=136
RECORD_DAY="2026-10-30"
EXPECTED_PRE_EX_DAY="2026-10-28"
ASSUMED_EX_DAY="2026-10-29"
ISSUER_NOTICE_URL="https://www.samsungfund.com/etf/lounge/notice-view.do?no=80553"
GAP_DAYS=("2026-10-22","2026-10-26","2026-10-27","2026-10-28")
STATUS="ETF_DISTRIBUTION_PRICE_FACTOR_CONSISTENT_NOT_VENDOR_METHOD_PROVEN"
MAX_ABS_PRICE_RESIDUAL_KRW=1.1
FIELDS=("open","high","low","close")


def _checked_gap_report(source:Path, observations:Path, history:Path)->tuple[dict,str]:
    if source.suffix!=".json":
        raise ValueError("Pinned stock gap JSON is required")
    digest=source.with_suffix(".json.sha256")
    payload=source.read_bytes()
    if len(payload)>2_000_000 or sha256(payload).hexdigest()!=digest.read_text().strip():
        raise ValueError("Stock gap report SHA mismatch")
    stored=json.loads(payload)
    recalculated=inspect(observations=observations,history=history,since="2026-10-20")
    if stored!=recalculated:
        raise ValueError("Gap report not identical to independent source DB audit")
    if (stored.get("status")!="VENDOR_HISTORY_GAP_PROVENANCE_UNRESOLVED_NO_TRADING" or
        stored.get("broker_orders")!=0 or
        stored.get("paper_or_live_trading_eligible") is not False or
        stored.get("raw_source_sha256_checked_for_every_conflict_variant") is not True):
        raise ValueError("Unexpected stock gap provenance status")
    return stored,sha256(payload).hexdigest()


def study(gap:dict,gap_sha:str)->dict:
    if (gap.get("status")!="VENDOR_HISTORY_GAP_PROVENANCE_UNRESOLVED_NO_TRADING" or
        gap.get("broker_orders")!=0 or
        gap.get("missing_days_are_not_automatically_backfilled") is not True):
        raise ValueError("Only immutable unresolved stock gap evidence accepted")
    rows=gap.get("gaps")
    if not isinstance(rows,list):
        raise ValueError("Missing vendor gap source rows")
    etf={r["session"]:r for r in rows if r.get("symbol")==TICKER}
    if set(etf)!=set(GAP_DAYS) or any(r.get("market")!="KRX" for r in etf.values()):
        raise ValueError("Expected four KODEX 200 raw gaps not present")
    oct28=etf[EXPECTED_PRE_EX_DAY]
    observed_close=oct28["sources"][0]["observed_ohlc"][3]
    if (type(observed_close) not in (float,int) or
        not math.isclose(observed_close,95650.,abs_tol=1e-8) or
        observed_close<=DISTRIBUTION_KRW):
        raise ValueError("Unexpected pre-distribution observed ETF close")
    factor=(observed_close-DISTRIBUTION_KRW)/observed_close
    comparisons=[]
    for day in GAP_DAYS:
        entry=etf[day]
        if (entry.get("latest_conflict_classification")!=
                "COHERENT_OHLC_SCALE_SHIFT_POSSIBLE_ADJUSTMENT_NOT_VERIFIED" or
            not entry.get("material_price_conflict_unresolved") or
            not isinstance(entry.get("sources"),list) or
            not entry["sources"]):
            raise ValueError("ETFs must remain source-conflicted")
        latest=entry["sources"][0]
        observed=latest["observed_ohlc"]
        vendor=latest["vendor_ohlc"]
        if (len(observed)!=4 or len(vendor)!=4 or
            any(type(x) not in (float,int) or
                not math.isfinite(x) or x<=0 for x in observed+vendor)):
            raise ValueError("Invalid OHLC evidence in gap report")
        hypothetical=[float(x)*factor for x in observed]
        residual=[float(v)-p for v,p in zip(vendor,hypothetical)]
        residual_abs=max(abs(v) for v in residual)
        if residual_abs>MAX_ABS_PRICE_RESIDUAL_KRW:
            raise ValueError("Official-distribution hypothesis does not explain OHLC")
        comparisons.append({
            "date":day,
            "source_vendor_raw_sha256":latest["raw_sha256"],
            "earlier_source_agreed_with_unadjusted_observed_ohlc":
                bool(entry.get("earlier_agreeing_price_snapshot_seen")),
            "observed_unadjusted_ohlc":observed,
            "vendor_latest_adjusted_like_ohlc":vendor,
            "cash_distribution_ratio_hypothesis_ohlc":hypothetical,
            "vendor_minus_hypothesis_krw_by_ohlc":residual,
            "max_abs_residual_krw":residual_abs,
            "relative_vendor_close_vs_observed_bps":
                10_000*(vendor[3]/observed[3]-1),
            "volume_conflict_explained_by_distribution":False,
            "research_classification":
                "CONSISTENT_WITH_136KRW_DISTRIBUTION_FACTOR_NOT_FORMULA_PROVEN",
            "eligible_for_history_rewrite":False,
        })
    us=[]
    for item in rows:
        if item.get("symbol")==TICKER:
            continue
        if item.get("market")!="NASDAQ_NYSE":
            raise ValueError("Unknown unexpected other market gap")
        us.append({
            "symbol":item["symbol"],
            "date":item["session"],
            "classification":item["latest_conflict_classification"],
            "vendor_raw_sha256":item["sources"][0]["raw_sha256"],
            "no_etf_distribution_adjustment_applied":True,
            "requires_independent_trade_price_reference":True,
            "eligible_for_history_rewrite":False,
        })
    if len(us)!=5:
        raise ValueError("Expected five unresolved US stock gaps")
    return {
        "status":STATUS,
        "source_gap_provenance_sha256":gap_sha,
        "source_gap_report_reindependently_verified_against_readonly_stock_dbs":True,
        "issuer_distribution_evidence":{
            "issuer":ISSUER,"symbol":TICKER,
            "notice_published_date":PUBLISHED_UTC_DAY,
            "official_notice_url":ISSUER_NOTICE_URL,
            "cash_distribution_per_unit_krw":DISTRIBUTION_KRW,
            "reference_day":EXPECTED_PRE_EX_DAY,
            "reference_observed_close_krw":observed_close,
            "distribution_record_day":RECORD_DAY,
            "ex_day_inferred_for_hypothesis_only":ASSUMED_EX_DAY,
            "hypothetical_multiplier":factor,
            "formula":"(pre_ex_close - issuer_cash_distribution) / pre_ex_close",
            "issuer_confirms_cash_distribution_not_source_adjustment_algorithm":True,
        },
        "total_etf_gap_sessions_with_consistent_factor":len(comparisons),
        "etf_ohlc_observations_compared":4*len(comparisons),
        "max_abs_etf_ohlc_residual_krw":max(
            x["max_abs_residual_krw"] for x in comparisons),
        "etf_vendor_algorithm_independently_confirmed":False,
        "etf_historical_executable_price_certified":False,
        "us_missing_days_not_explained_by_etf_distribution":len(us),
        "overall_unresolved_missing_stock_sessions":len(rows),
        "missing_days_replaced_in_long_history":0,
        "historic_buy_sell_signals_recalculated":False,
        "broker_orders":0,
        "paper_or_live_trading_eligible":False,
        "certified_strategy_returns":None,
        "etf_comparisons":comparisons,
        "us_unresolved":sorted(us,key=lambda x:(x["symbol"],x["date"])),
    }


def analyze(*,gap_report:Path|str,observations:Path|str=DEFAULT_OBSERVATIONS,
            history:Path|str=DEFAULT_HISTORY,output_dir:Path|str)->dict:
    gap,digest=_checked_gap_report(
        Path(gap_report).expanduser().resolve(),
        Path(observations).expanduser().resolve(),
        Path(history).expanduser().resolve())
    report=study(gap,digest)
    out=Path(output_dir).expanduser().resolve()
    if out.exists():
        raise ValueError("Immutable ETF study output already exists")
    out.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".etf-dist-",dir=out.parent))
    try:
        os.chmod(stage,0o700)
        data=(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        target=stage/"study.json"
        target.write_bytes(data)
        target.chmod(0o600)
        checksum=stage/"study.sha256"
        checksum.write_text(sha256(data).hexdigest()+"\n")
        checksum.chmod(0o600)
        if out.exists():
            raise ValueError("Concurrent ETF study output exists")
        stage.rename(out)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return report


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="Independent ETF distribution sidecar, read-only")
    p.add_argument("--gap-report",required=True)
    p.add_argument("--observations",default=DEFAULT_OBSERVATIONS)
    p.add_argument("--history",default=DEFAULT_HISTORY)
    p.add_argument("--output-dir",required=True)
    args=p.parse_args(argv)
    try:
        r=analyze(gap_report=args.gap_report,observations=args.observations,
                  history=args.history,output_dir=args.output_dir)
        print(json.dumps({
            "status":r["status"],
            "etf_gap_days":r["total_etf_gap_sessions_with_consistent_factor"],
            "max_abs_ohlc_residual_krw":r["max_abs_etf_ohlc_residual_krw"],
            "us_unresolved_days":r["us_missing_days_not_explained_by_etf_distribution"],
            "past_bars_replaced":r["missing_days_replaced_in_long_history"],
            "actual_orders":r["broker_orders"],
            "live_approved":r["paper_or_live_trading_eligible"],
        },ensure_ascii=False))
        return 0
    except (ValueError,TypeError,KeyError,OSError):
        print(json.dumps({"status":"ETF_RESEARCH_FAIL_CLOSED_NO_HISTORICAL_FILL_PROOF",
                          "actual_orders":0,"live_approved":False}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

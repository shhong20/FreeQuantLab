"""Versioned, non-destructive stock price *variant* dataset (research quarantine only).

Four KODEX 200 missing sessions retain original observed, later vendor, and
136-KRW cash distribution *hypothesis* as distinct labeled variants. Five
unresolved US equity sessions are metadata-only and do not inherit the ETF
adjustment. Nothing here is a canonical historical trading price.
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

from .stock_etf_distribution_study import study, _checked_gap_report
from .stock_gap_provenance import DEFAULT_OBSERVATIONS, DEFAULT_HISTORY

VERSION="KODEX200_DISTRIBUTION_CANDIDATES_V1_20261103"
STATUS="SOURCE_PINNED_PRICE_VARIANTS_RESEARCH_QUARANTINED_NOT_BACKTEST_READY"
VARIANTS=("original_first_observed","latest_vendor_archived",
          "issuer_cash_distribution_ratio_hypothesis")
OUTPUT_FILES=("etf_variants.jsonl","unresolved_us.jsonl")
PARENT_STUDY_NAME="study.json"


def _serialise_jsonl(records:list[dict])->bytes:
    return b"".join((json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n").encode()
                    for row in records)


def _sha(data:bytes)->str:
    return sha256(data).hexdigest()


def _read_pinned_study(
    study_dir:Path,observations:Path,history:Path,gap_path:Path
)->tuple[dict,str,str]:
    p=study_dir/PARENT_STUDY_NAME
    raw=p.read_bytes()
    if (not raw or len(raw)>2_000_000 or _sha(raw)!=(study_dir/"study.sha256").read_text().strip()):
        raise ValueError("ETF study source SHA mismatch")
    original=json.loads(raw)
    gap,gap_digest=_checked_gap_report(gap_path,observations,history)
    rederived=study(gap,gap_digest)
    if original!=rederived:
        raise ValueError("ETF study does not match current independently verified raw sources")
    if (original["source_gap_report_reindependently_verified_against_readonly_stock_dbs"] is not True or
        original["etf_vendor_algorithm_independently_confirmed"] is not False or
        original["etf_historical_executable_price_certified"] is not False or
        original["missing_days_replaced_in_long_history"]!=0 or
        original["paper_or_live_trading_eligible"] is not False or
        original["certified_strategy_returns"] is not None or
        original["broker_orders"]!=0):
        raise ValueError("Source study not eligible for price variant quarantine")
    return original,_sha(raw),gap_digest


def _number_array(value:object)->list[float]:
    if (not isinstance(value,list) or len(value)!=4 or
        any(type(v) not in (float,int) or not math.isfinite(v) or v<=0
            for v in value)):
        raise ValueError("Invalid OHLC candidate")
    return [float(v) for v in value]


def _create_rows(source:dict)->tuple[list[dict],list[dict]]:
    evidence=source["issuer_distribution_evidence"]
    factor=evidence["hypothetical_multiplier"]
    if (type(factor) is not float or not 0<factor<1 or
        evidence["symbol"]!="069500" or
        evidence["cash_distribution_per_unit_krw"]!=136 or
        evidence["reference_day"]!="2026-10-28" or
        evidence["reference_observed_close_krw"]!=95650 or
        not math.isclose(factor,(95650-136)/95650,rel_tol=0,abs_tol=1e-13)):
        raise ValueError("Unexpected ETF scenario factor")
    input_rows=source.get("etf_comparisons")
    if not isinstance(input_rows,list) or len(input_rows)!=4:
        raise ValueError("Missing four ETF variant dates")
    etf=[]
    for rec in input_rows:
        original=_number_array(rec["observed_unadjusted_ohlc"])
        vendor=_number_array(rec["vendor_latest_adjusted_like_ohlc"])
        hypothetical=_number_array(rec["cash_distribution_ratio_hypothesis_ohlc"])
        if (len(rec["source_vendor_raw_sha256"])!=64 or
            rec["eligible_for_history_rewrite"] is not False or
            rec["volume_conflict_explained_by_distribution"] is not False):
            raise ValueError("Source ETF variance not safely quarantined")
        residual=[float(v)-float(h) for v,h in zip(vendor,hypothetical)]
        if (max(abs(x) for x in residual)>1.1 or
            any(not math.isclose(float(a)*factor,float(b),rel_tol=0,abs_tol=1e-8)
                for a,b in zip(original,hypothetical)) or
            any(not math.isclose(float(a),float(b),rel_tol=0,abs_tol=1e-8)
                for a,b in zip(rec["vendor_minus_hypothesis_krw_by_ohlc"],residual))):
            raise ValueError("Distribution price candidate not independently reproducible")
        etf.append({
            "schema_version":VERSION,
            "market":"KRX","symbol":"069500","session":rec["date"],
            "source_vendor_raw_sha256":rec["source_vendor_raw_sha256"],
            "earlier_source_agreed_with_first_observation":
                rec["earlier_source_agreed_with_unadjusted_observed_ohlc"],
            "variants":[
                {"variant_id":VARIANTS[0],"basis":"immutable_initial_observation",
                 "ohlc_krw":original,"historical_executable_price_certified":False},
                {"variant_id":VARIANTS[1],"basis":"later_immutable_vendor_raw",
                 "ohlc_krw":vendor,"historical_executable_price_certified":False},
                {"variant_id":VARIANTS[2],"basis":"issuer_136krw_factor_hypothesis_not_vendor_confirmed",
                 "ohlc_krw":hypothetical,"historical_executable_price_certified":False},
            ],
            "issuer_distribution_krw":136,
            "hypothesized_factor":factor,
            "vendor_minus_hypothesis_krw_by_ohlc":residual,
            "max_abs_residual_krw":max(abs(x) for x in residual),
            "missing_from_primary_long_history":True,
            "canonical_trade_price_selected":False,
            "price_adjustment_algorithm_confirmed":False,
            "eligible_for_training_validation_test":False,
            "eligible_for_backtesting_paper_live":False,
        })
    etf.sort(key=lambda r:r["session"])
    if [r["session"] for r in etf] != [
        "2026-10-22","2026-10-26","2026-10-27","2026-10-28"]:
        raise ValueError("Unexpected 4 ETF gap dates")
    us=[]
    for rec in source.get("us_unresolved",[]):
        if (rec["symbol"] not in ("AAPL","MSFT","SPY") or
            rec["eligible_for_history_rewrite"] is not False or
            rec["requires_independent_trade_price_reference"] is not True or
            rec["no_etf_distribution_adjustment_applied"] is not True or
            len(rec["vendor_raw_sha256"])!=64):
            raise ValueError("Non-ETF unresolved source not safely quarantined")
        us.append({
            "schema_version":VERSION,"market":"NASDAQ_NYSE",
            "symbol":rec["symbol"],"session":rec["date"],
            "latest_conflict_classification":rec["classification"],
            "source_vendor_raw_sha256":rec["vendor_raw_sha256"],
            "distribution_factor_applied":False,
            "canonical_trade_price_selected":False,
            "requires_independent_price_reference":True,
            "eligible_for_training_validation_test":False,
            "eligible_for_backtesting_paper_live":False,
        })
    us.sort(key=lambda r:(r["symbol"],r["session"]))
    if len(us)!=5 or len({(r["symbol"],r["session"]) for r in us})!=5:
        raise ValueError("Expected five unique unresolved US stock gap days")
    return etf,us


def _manifest(etf:list[dict],us:list[dict],
              study_sha:str,gap_sha:str,payloads:dict[str,bytes])->dict:
    return {
        "schema_version":VERSION,
        "status":STATUS,
        "source_immutable_etf_study_sha256":study_sha,
        "source_immutable_stock_gap_report_sha256":gap_sha,
        "official_issuer_notice_url":
            "https://www.samsungfund.com/etf/lounge/notice-view.do?no=80553",
        "scenario_is_not_official_vendor_adjustment":True,
        "independent_vendor_adjustment_method_verified":False,
        "data_quality":"RESEARCH_QUARANTINE_PRICE_VARIANTS_ONLY",
        "etf_sessions":len(etf),"etf_ohlc_variant_records":len(etf)*3,
        "us_unresolved_sessions":len(us),
        "price_variant_selection_for_trading":"NONE",
        "filled_missing_primary_history_days":0,
        "historical_returns_computed":False,
        "approved_for_model_training":False,
        "approved_for_backtest":False,
        "approved_for_paper_live":False,
        "broker_orders":0,
        "files":{k:{"sha256":_sha(v),"bytes":len(v),
                    "records":len(etf) if k=="etf_variants.jsonl" else len(us)}
                 for k,v in sorted(payloads.items())},
    }


def build(*,study_dir:Path|str,gap_report:Path|str,
          output_dir:Path|str,
          observations:Path|str=DEFAULT_OBSERVATIONS,
          history:Path|str=DEFAULT_HISTORY)->dict:
    study_source,study_sha,gap_sha=_read_pinned_study(
        Path(study_dir).expanduser().resolve(),
        Path(observations).expanduser().resolve(),
        Path(history).expanduser().resolve(),
        Path(gap_report).expanduser().resolve())
    etf,us=_create_rows(study_source)
    payloads={"etf_variants.jsonl":_serialise_jsonl(etf),
              "unresolved_us.jsonl":_serialise_jsonl(us)}
    manifest=_manifest(etf,us,study_sha,gap_sha,payloads)
    target=Path(output_dir).expanduser().resolve()
    if target.exists():
        raise ValueError("Versioned research dataset exists; no overwrite")
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".etf-variants-",dir=target.parent))
    try:
        stage.chmod(0o700)
        for filename,data in payloads.items():
            path=stage/filename
            path.write_bytes(data)
            path.chmod(0o600)
        raw=(json.dumps(manifest,sort_keys=True,ensure_ascii=False,indent=2)+"\n").encode()
        (stage/"manifest.json").write_bytes(raw)
        (stage/"manifest.json").chmod(0o600)
        (stage/"manifest.sha256").write_text(_sha(raw)+"\n")
        (stage/"manifest.sha256").chmod(0o600)
        if target.exists():
            raise ValueError("Concurrent versioned research dataset exists")
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    verify(output_dir=target,study_dir=study_dir,gap_report=gap_report,
           observations=observations,history=history)
    return manifest


def verify(*,output_dir:Path|str,study_dir:Path|str,gap_report:Path|str,
           observations:Path|str=DEFAULT_OBSERVATIONS,
           history:Path|str=DEFAULT_HISTORY)->dict:
    root=Path(output_dir).expanduser().resolve()
    manifest_payload=(root/"manifest.json").read_bytes()
    if _sha(manifest_payload)!=(root/"manifest.sha256").read_text().strip():
        raise ValueError("Versioned research manifest SHA mismatch")
    recorded=json.loads(manifest_payload)
    study_source,study_sha,gap_sha=_read_pinned_study(
        Path(study_dir).expanduser().resolve(),
        Path(observations).expanduser().resolve(),
        Path(history).expanduser().resolve(),
        Path(gap_report).expanduser().resolve())
    etf,us=_create_rows(study_source)
    expected_data={"etf_variants.jsonl":_serialise_jsonl(etf),
                   "unresolved_us.jsonl":_serialise_jsonl(us)}
    expected_manifest=_manifest(etf,us,study_sha,gap_sha,expected_data)
    if recorded!=expected_manifest:
        raise ValueError("Versioned research manifest or provenance changed")
    for filename,expected in expected_data.items():
        raw=(root/filename).read_bytes()
        if raw!=expected or _sha(raw)!=recorded["files"][filename]["sha256"]:
            raise ValueError("Versioned research payload differs from independently verified sources")
    if root.stat().st_mode&0o777!=0o700 or any(
        (root/name).stat().st_mode&0o777!=0o600 for name in (
            *OUTPUT_FILES,"manifest.json","manifest.sha256")):
        raise ValueError("Research variant dataset permissions invalid")
    return recorded


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="Research-only variant dataset; no backtest/live orders")
    p.add_argument("--study-dir",required=True)
    p.add_argument("--gap-report",required=True)
    p.add_argument("--output-dir",required=True)
    p.add_argument("--observations",default=DEFAULT_OBSERVATIONS)
    p.add_argument("--history",default=DEFAULT_HISTORY)
    p.add_argument("--verify-only",action="store_true")
    args=p.parse_args(argv)
    try:
        kw=dict(output_dir=args.output_dir,study_dir=args.study_dir,
                gap_report=args.gap_report,observations=args.observations,
                history=args.history)
        result=verify(**kw) if args.verify_only else build(**kw)
        print(json.dumps({
            "status":result["status"],"version":result["schema_version"],
            "etf_sessions":result["etf_sessions"],
            "us_unresolved_sessions":result["us_unresolved_sessions"],
            "backtest_approved":result["approved_for_backtest"],
            "paper_live_approved":result["approved_for_paper_live"],
            "real_orders":result["broker_orders"],
        },ensure_ascii=False,sort_keys=True))
        return 0
    except (KeyError,TypeError,ValueError,OSError):
        print(json.dumps({"status":"RESEARCH_VARIANT_DATASET_FAIL_CLOSED",
                          "backtest_approved":False,"paper_live_approved":False,
                          "real_orders":0}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())

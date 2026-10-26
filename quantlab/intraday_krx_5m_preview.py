"""Provisional, provenance-checked KRX continuous-session 1m to 5m previews.

No timestamp semantics certification, training, strategy selection, P&L,
portfolio ledger changes, or broker calls. Existing intraday_research
requires >=60 sessions, start-labeled bars and separately verified policy.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

from .intraday_kis_cohort_capture import DEFAULT_DATES, DEFAULT_SYMBOLS, _comma_list, plan
from .intraday_kis_venue_compare import _read_one
from .intraday_kis_venues import VenueProbeError

CLOCKS = tuple(f"{t//60:02d}{t%60:02d}00" for t in range(9*60, 15*60+20))
MARKET = "J"
FIELDS = {
    "open": "stck_oprc",
    "high": "stck_hgpr",
    "low": "stck_lwpr",
    "close": "stck_prpr",
    "volume": "cntg_vol",
}
MIN_SESSIONS_FOR_EXISTING_ENGINE = 60


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)+"\n").encode()


def candidate(root: Path, *, symbol: str, session: str) -> dict:
    audit, observations = _read_one(root, symbol, session, MARKET)
    if not set(CLOCKS).issubset(observations):
        missing = [label for label in CLOCKS if label not in observations]
        raise VenueProbeError(
            "KRX continuous trading window incomplete; missing labels: "+
            str(len(missing))
        )
    if "153000" not in observations:
        raise VenueProbeError("Separate KRX 15:30 auction marker must be present")
    normalized = {}
    for label in CLOCKS:
        item = observations[label]
        if item.get("stck_bsop_date") != session.replace("-",""):
            raise VenueProbeError("Minute observation belongs to different session")
        try:
            values = {name: int(item[field]) for name,field in FIELDS.items()}
        except (ValueError,TypeError,OverflowError) as exc:
            raise VenueProbeError("Unusable minute OHLCV") from exc
        if (min(values["open"],values["high"],values["low"],values["close"]) <= 0
                or values["volume"] < 0
                or values["high"] < max(values["open"], values["low"], values["close"])
                or values["low"] > min(values["open"],values["high"],values["close"])):
            raise VenueProbeError("Contradictory KRX source OHLCV")
        normalized[label]=values
    bars = []
    for ix in range(0,len(CLOCKS),5):
        batch=[normalized[t] for t in CLOCKS[ix:ix+5]]
        start=CLOCKS[ix]
        bars.append({
            "timestamp":f"{session}T{start[:2]}:{start[2:4]}:00+09:00",
            "open":batch[0]["open"],
            "high":max(x["high"] for x in batch),
            "low":min(x["low"] for x in batch),
            "close":batch[-1]["close"],
            "volume":sum(x["volume"] for x in batch),
        })
    if len(bars)!=76 or bars[0]["timestamp"][11:16]!="09:00" or (
            bars[-1]["timestamp"][11:16]!="15:15"):
        raise VenueProbeError("Only 76 complete KRX regular 5m buckets permitted")
    minute_total=sum(row["volume"] for row in normalized.values())
    if minute_total!=sum(row["volume"] for row in bars):
        raise VenueProbeError("5m aggregate volume does not conserve 1m data")
    # The separate 15:30 auction and after-hours trades are not continuous
    # 5-minute bars. Their prices and volumes must never enter these buckets.
    day=audit.get("daily_comparison",{})
    daily_rows=day.get("date_only_public_fields",[])
    daily=(
        int(daily_rows[0]["acml_vol"])
        if day.get("requested_date_matches")==1 and
        isinstance(daily_rows,list) and len(daily_rows)==1 and
        isinstance(daily_rows[0],dict) and
        str(daily_rows[0].get("acml_vol","")).isdecimal()
        else None
    )
    return {
        "symbol":symbol, "session":session, "venue":"KRX_J",
        "source_sha256":audit["source_minute_bar_sha256"],
        "source_minute_labels":380, "candidate_5m_labels":76,
        "candidate_5m_sha256":sha256(_json_bytes(bars)).hexdigest(),
        "regular_1m_volume":minute_total,
        "candidate_5m_volume":sum(b["volume"] for b in bars),
        "official_daily_volume":daily,
        "official_daily_minus_regular_volume":(
            daily-minute_total if daily is not None else None
        ),
        "closing_auction_153000_excluded":True,
        "closing_auction_marker_observed":True,
        "after_hours_excluded":True,
        "original_minute_timestamp_start_end_vendor_verified":False,
        "timestamp_start_is_provisional_assumption":True,
        "suitable_for_order_or_pnl":False,
        "bars":bars,
    }


def build(*, source_root: str | Path, out_dir: str | Path,
          symbols: tuple[str,...], sessions: tuple[str,...],
          baseline_root: str | Path | None = None) -> dict:
    plan(symbols=",".join(symbols),sessions=",".join(sessions),markets="J")
    # Existing intraday_research.validate requires strictly chronological
    # 5-minute timestamps. Don't silently reorder caller-provided sessions.
    if tuple(sessions) != tuple(sorted(sessions)):
        raise VenueProbeError("KRX 5m research sessions must be chronological")
    root=Path(source_root).expanduser().resolve()
    baseline=Path(baseline_root).expanduser().resolve() if baseline_root else None
    target=Path(out_dir).expanduser().resolve()
    if (target/"candidate_5m_summary.json").exists():
        raise VenueProbeError("Immutable candidate 5m report already exists")
    report=[]
    grouped:dict[str,list[dict]]={sym:[] for sym in symbols}
    for symbol in symbols:
        for session in sessions:
            path=root/f"KIS_J_EXT_{symbol}_{session}"
            if not path.is_dir():
                raise VenueProbeError("Required full KRX snapshot missing (no zero-fill)")
            current=candidate(root,symbol=symbol,session=session)
            previous_path=(
                baseline/f"KIS_J_EXT_{symbol}_{session}"
                if baseline is not None else None
            )
            prior=None
            if previous_path is not None and previous_path.is_dir():
                prior=candidate(baseline,symbol=symbol,session=session)
            revised=None
            if prior is not None:
                revised={
                    "comparison":"INDEPENDENT_OLD_AND_NEW_KRX_SNAPSHOTS",
                    "minute_source_sha256_equal":(
                        prior["source_sha256"]==current["source_sha256"]
                    ),
                    "all_76_5m_ohlcv_identical":(
                        prior["candidate_5m_sha256"]==current["candidate_5m_sha256"]
                    ),
                    "changed_5m_bar_count":sum(
                        a!=b for a,b in zip(prior["bars"],current["bars"])
                    ),
                    "old_5m_sha256":prior["candidate_5m_sha256"],
                    "new_5m_sha256":current["candidate_5m_sha256"],
                    "timestamp_semantics_still_unverified":True,
                }
            report.append({k:v for k,v in current.items() if k!="bars"} |
                          {"independent_replay_comparison":revised,
                           "replay_comparable":prior is not None})
            grouped[symbol].extend(current["bars"])
    readiness={}
    for symbol, bars in grouped.items():
        observed=len(bars)//76
        readiness[symbol]={
            "source_sessions":observed,
            "continuous_5m_candles":len(bars),
            "existing_intraday_engine_required_completed_sessions":
                MIN_SESSIONS_FOR_EXISTING_ENGINE,
            "sessions_missing_to_minimum":max(
                0, MIN_SESSIONS_FOR_EXISTING_ENGINE-observed
            ),
            "meeting_engine_minimum_sessions":(
                observed>=MIN_SESSIONS_FOR_EXISTING_ENGINE
            ),
            "timestamp_label_contract_vendor_verified":False,
            "corporate_action_and_auction_contract_verified":False,
            "eligible_to_call_intraday_research_evaluate":False,
            "walkforward_pnl_status":"HOLD_NO_OOS_STRATEGY_PNL",
        }
    summary={
        "status":"PROVISIONAL_KRX_CONTINUOUS_5M_SOURCE_HOLD",
        "source_venue":"J_ONLY_NO_NXT_OR_UN_COMBINING",
        "symbols":list(symbols),
        "sessions":list(sessions),
        "complete_symbol_session_cases":len(report),
        "total_candidate_regular_5m_candles":sum(len(x) for x in grouped.values()),
        "independent_baseline_case_count":sum(
            x["replay_comparable"] for x in report
        ),
        "independent_5m_revision_case_count":sum(
            x["independent_replay_comparison"] is not None and
            not x["independent_replay_comparison"]["all_76_5m_ohlcv_identical"]
            for x in report
        ),
        "case_results":report,
        "readiness_by_symbol":readiness,
        "timestamp_semantics_unverified":True,
        "no_close_auction_or_after_hours_5m_imputation":True,
        "source_bar_revisions_do_not_authorize_trading":True,
        "strategy_selection_performed":False,
        "walkforward_return_calculated":False,
        "usable_for_backtesting":False,
        "usable_for_paper_or_live":False,
    }
    target.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".krx-5m-",dir=target))
    try:
        for symbol,bars in grouped.items():
            # JSON candidates rather than operational .csv prevents accidental
            # passing to the standalone intraday strategy CLI.
            data=_json_bytes(bars)
            (stage/f"{symbol}_candidate_5m_UNVERIFIED.json").write_bytes(data)
            (stage/f"{symbol}_candidate_5m_UNVERIFIED.sha256").write_text(
                sha256(data).hexdigest()+"\n",encoding="utf-8")
        s=_json_bytes(summary)
        (stage/"candidate_5m_summary.json").write_bytes(s)
        (stage/"candidate_5m_summary.sha256").write_text(
            sha256(s).hexdigest()+"\n",encoding="utf-8")
        if (target/"candidate_5m_summary.json").exists():
            raise VenueProbeError("Concurrent immutable 5m report")
        for file in stage.iterdir():
            if (target/file.name).exists():
                raise VenueProbeError("Existing candidate file cannot be overwritten")
        # Persist source candidate first and summary last as commit marker.
        for file in sorted(stage.iterdir(),key=lambda p:
                           p.name=="candidate_5m_summary.json"):
            file.rename(target/file.name)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"summary":summary,
            "output":str(target/"candidate_5m_summary.json")}


def main(argv: list[str] | None=None) -> int:
    p=argparse.ArgumentParser(description="Offline 1m-to-5m KRX provenance preview, no backtest")
    p.add_argument("--source-root",required=True)
    p.add_argument("--baseline-root")
    p.add_argument("--out-dir",required=True)
    p.add_argument("--symbols",default=DEFAULT_SYMBOLS)
    p.add_argument("--sessions",default=DEFAULT_DATES)
    a=p.parse_args(argv)
    try:
        result=build(source_root=a.source_root,
                     baseline_root=a.baseline_root,out_dir=a.out_dir,
                     symbols=_comma_list(a.symbols),
                     sessions=_comma_list(a.sessions))
        report=result["summary"]
        print(json.dumps({
            "output":result["output"],"status":report["status"],
            "complete_symbol_session_cases":report["complete_symbol_session_cases"],
            "total_candidate_regular_5m_candles":
                report["total_candidate_regular_5m_candles"],
            "independent_baseline_case_count":
                report["independent_baseline_case_count"],
            "independent_5m_revision_case_count":
                report["independent_5m_revision_case_count"],
            "readiness_by_symbol":report["readiness_by_symbol"],
            "usable_for_backtesting":False,
        },sort_keys=True,ensure_ascii=False,indent=2))
        return 0
    except VenueProbeError:
        p.exit(2,"FAIL CLOSED: VenueProbeError (source integrity or complete regular session)\n")


if __name__=="__main__":
    raise SystemExit(main())

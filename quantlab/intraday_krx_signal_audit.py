"""Hash-checked KRX strategy signal inventory; never PnL or simulated fills.

All halted/incomplete stock-days remain present and blocked. Only complete,
nonhalt provisional days can produce signal *observations*. Vendor clock
semantics remain unverified, so no strategy can be promoted.
"""
from __future__ import annotations
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
import shutil
import tempfile
from .intraday_krx_halt_position_stress import _read_verified, _validated
from .intraday_kis_venues import VenueProbeError

CLEAN = "PROVISIONAL_COMPLETE_DAY_VENDOR_SEMANTICS_HOLD"
NAMES = ("orb_5m_rvol1.5", "orb_15m_rvol1.5", "orb_30m_rvol1.5",
         "intraday_momentum_30m")

def audit(input_root: str | Path, out_dir: str | Path) -> dict:
    source, output = Path(input_root).resolve(), Path(out_dir).resolve()
    if output.exists():
        raise VenueProbeError("Immutable signal report already exists")
    summary = _read_verified(source, "halt_5m_summary.json")
    if (not isinstance(summary, dict) or
        summary.get("status") != "HALT_AWARE_5M_CANDIDATES_RESEARCH_ONLY_NO_PNL" or
        summary.get("usable_for_backtesting") is not False or
        summary.get("usable_for_paper_or_live") is not False):
        raise VenueProbeError("Not a risk-gated historical source")
    sessions, symbols = summary.get("sessions"), summary.get("symbols")
    if (not isinstance(sessions, list) or not 3 <= len(sessions) <= 120 or
        sessions != sorted(set(sessions)) or
        not isinstance(symbols, list) or not 1 <= len(symbols) <= 3 or
        len(set(symbols)) != len(symbols) or
        summary.get("total_stock_days") != len(sessions)*len(symbols) or
        summary.get("total_5m_clock_slots") != 76*len(sessions)*len(symbols)):
        raise VenueProbeError("Incorrect risk population")
    split1, split2 = int(.6*len(sessions)), int(.8*len(sessions))
    windows = {s: ("train" if i < split1 else
                    "validation" if i < split2 else "untouched_test")
               for i, s in enumerate(sessions)}
    records, totals, by_symbol = [], Counter(), {}
    for symbol in symbols:
        days = _validated(source, symbol, sessions)
        prior = {n: [] for n in (1,3,6)}
        dispositions = Counter()
        signals_by_window = {w: Counter() for w in
                             ("train","validation","untouched_test")}
        for day in days:
            status, session = day["day_disposition"], day["session"]
            totals[status] += 1
            dispositions[status] += 1
            allowed = status == CLEAN and day.get("reported_cb") is None
            slots = day["slots"]
            full = all(s.get("ohlcv") is not None and
                       s.get("source_observed_count") == 5 and
                       s.get("market_reported_cb_window_overlap") is False
                       for s in slots)
            if allowed and (not full or
                            day.get("closing_153000_label_present") is not True):
                raise VenueProbeError("Unsafe day mislabeled as complete")
            names = []
            if allowed:
                bars = [s["ohlcv"] for s in slots]
                for n in (1,3,6):
                    previous = prior[n][-20:]
                    start_vol = sum(b["volume"] for b in bars[:n])
                    if len(previous) >= 10 and median(previous) > 0 and (
                        start_vol >= 1.5*median(previous)
                    ):
                        threshold = max(b["high"] for b in bars[:n])
                        if any(bars[i]["close"] > threshold
                               for i in range(n, len(bars)-2)):
                            names.append(f"orb_{n*5}m_rvol1.5")
                    prior[n].append(start_vol)
                if bars[5]["close"] >= 1.002*bars[0]["open"]:
                    names.append("intraday_momentum_30m")
                for name in names:
                    signals_by_window[windows[session]][name] += 1
            records.append({"symbol":symbol, "session":session,
                            "source_minute_sha256":day["source_minute_sha256"],
                            "window":windows[session], "day_disposition":status,
                            "all_76_slots_preserved":len(slots)==76,
                            "signal_observation_allowed":allowed,
                            "signal_names_not_trades":names,
                            "simulated_fill_count":0, "strategy_return":None})
        by_symbol[symbol] = {
            "source_days":len(days), "candidate_full_days":dispositions[CLEAN],
            "blocked_days":len(days)-dispositions[CLEAN],
            "day_dispositions":dict(dispositions),
            "fixed_signal_counts_by_window":{
                w:{n:signals_by_window[w][n] for n in NAMES}
                for w in signals_by_window},
        }
    if dict(totals) != summary.get("day_dispositions"):
        raise VenueProbeError("Source day population disagrees with audit")
    report = {
        "status":"KRX_100DAY_SIGNAL_INVENTORY_ONLY_NO_PNL",
        "source_summary_sha256":sha256(
            (source/"halt_5m_summary.json").read_bytes()).hexdigest(),
        "symbols":symbols, "sessions":sessions,
        "window_ranges":{
            w:[x[0],x[-1]] for w in ("train","validation","untouched_test")
            if (x := [s for s in sessions if windows[s]==w])
        },
        "stock_days_preserved":len(records),
        "by_symbol":by_symbol, "day_records":records,
        "halt_and_unknown_days_not_dropped":True,
        "gap_strategy_disabled_until_corporate_actions_and_auction_verified":True,
        "unverified_timestamp_semantics_hold":True,
        "trade_or_fill_simulated":False, "strategy_selection_performed":False,
        "oos_returns_computed":False, "sharpe_or_mdd_computed":False,
        "usable_for_backtesting":False, "usable_for_paper_or_live":False,
    }
    output.parent.mkdir(parents=True,exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".krx-signal-",dir=output.parent))
    try:
        blob=(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (staging/"signal_audit.json").write_bytes(blob)
        (staging/"signal_audit.sha256").write_text(sha256(blob).hexdigest()+"\n")
        if output.exists():
            raise VenueProbeError("Concurrent immutable signal report")
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return report

def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description="KRX read-only strategy signals, no PnL")
    parser.add_argument("--input-root",required=True)
    parser.add_argument("--out-dir",required=True)
    args=parser.parse_args(argv)
    try:
        r=audit(args.input_root,args.out_dir)
        print(json.dumps({"status":r["status"],
                          "stock_days_preserved":r["stock_days_preserved"],
                          "by_symbol":r["by_symbol"]},ensure_ascii=False,indent=2))
        return 0
    except (VenueProbeError,TypeError,ValueError,KeyError):
        parser.exit(2,"FAIL CLOSED: unsafe input\n")

if __name__=="__main__":
    raise SystemExit(main())

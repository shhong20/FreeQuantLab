"""Regression tests for isolated equity intraday research (synthetic only)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from quantlab.intraday_research import (
    IntradayDataError, build_trades, evaluate, read_csv, validate,
)


SHA = "a" * 64


def bars(n=100, market="KRX_ETF"):
    # Synthetic completed full sessions, no claim of real profitability.
    days = pd.bdate_range(end="2026-09-30", periods=n)
    offset = "+09:00" if market != "US_EQUITY" else "-04:00"
    start = "09:00" if market != "US_EQUITY" else "09:30"
    records = []
    session_bars = 78 if market == "US_EQUITY" else 76
    for j, d in enumerate(days):
        first = datetime.combine(d.date(), datetime.strptime(start, "%H:%M").time())
        for i in range(session_bars):
            ts = (first + timedelta(minutes=5*i)).strftime("%Y-%m-%dT%H:%M:%S") + offset
            price = 100.0
            records.append({"timestamp": ts, "open": price, "high": 100.1,
                            "low": 99.9, "close": price, "volume": 100.0})
    return pd.DataFrame(records)


def setbar(df, day_i, bar_i, **kwargs):
    ix = day_i*76 + bar_i
    for name, value in kwargs.items():
        df.loc[ix, name] = value


def test_reject_timezone_gaps_bad_ohlc_duplicate_future_and_short_history():
    df = bars()
    validate(df, "KRX_STOCK")
    naive = df.copy()
    naive.loc[0, "timestamp"] = naive.loc[0, "timestamp"][:-6]
    with pytest.raises(IntradayDataError, match="offset"):
        validate(naive, "KRX_STOCK")
    missing = df.drop(index=9).reset_index(drop=True)
    with pytest.raises(IntradayDataError, match="Incomplete"):
        validate(missing, "KRX_STOCK")
    bad = df.copy()
    bad.loc[0, "low"] = 120
    with pytest.raises(IntradayDataError, match="Inconsistent"):
        validate(bad, "KRX_STOCK")
    dup = df.copy()
    dup.loc[2, "timestamp"] = dup.loc[1, "timestamp"]
    with pytest.raises(IntradayDataError, match="unique"):
        validate(dup, "KRX_STOCK")
    with pytest.raises(IntradayDataError, match="60"):
        validate(df.iloc[:59*76], "KRX_STOCK")
    bad_future = bars()
    bad_future.loc[75, "timestamp"] = "2099-01-01T15:25:00+09:00"
    with pytest.raises(IntradayDataError):
        validate(bad_future, "KRX_ETF")


def test_read_input_integrity_sha_and_tax_defaults(tmp_path):
    df = bars()
    csv = tmp_path / "bars.csv"
    df.to_csv(csv, index=False)
    parsed, digest = read_csv(csv, "KRX_STOCK")
    assert digest == __import__("hashlib").sha256(csv.read_bytes()).hexdigest()
    kr = evaluate(parsed, market="KRX_STOCK", source_sha256=digest)
    etf = evaluate(parsed, market="KRX_ETF", source_sha256=digest)
    assert kr["costs_bps"]["sell_tax_only"] == 20
    assert etf["costs_bps"]["sell_tax_only"] == 0
    assert kr["status"].endswith("NOT_PAPER_ELIGIBLE")
    assert kr["selected_by_validation_only"]["gap_reversion"] is None
    assert kr["gap_strategy_blocked_without_review"]
    assert kr["windows"]["validation"][1] < kr["windows"]["untouched_test"][0]


def test_orb_requires_prior_rvol_and_fills_next_open_not_breakout_close():
    df = bars()
    # Exactly one rvol spike after 15 quiet sessions.
    k = 15
    setbar(df, k, 0, volume=300)
    setbar(df, k, 1, high=102, low=99.9, close=101.5)
    setbar(df, k, 2, open=103, high=103.1, low=102.5, close=103)
    setbar(df, k, 75, open=104, high=104.1, low=103.8, close=104)
    v = validate(df, "KRX_ETF")
    trades = build_trades(v, fee_bps=0, slippage_bps=0)
    day = v.iloc[k*76]["session"]
    t = next(t for t in trades["orb_5m_rvol1.5"] if t["session"] == day)
    assert t["entry_open"] == 103
    assert t["exit_open"] == 104
    assert t["net_return"] == pytest.approx(104/103 - 1)
    assert not any(x["session"] == day for x in trades["orb_15m_rvol1.5"])
    # First 10 sessions can never use their own volume as the benchmark.
    assert all(x["session"] != v.iloc[0]["session"]
               for x in trades["orb_5m_rvol1.5"])


def test_long_gap_down_requires_operator_review_and_prev_completed_close():
    df = bars()
    k = 20
    # Prior last close exactly 100 (synthetic), next day gap down.
    setbar(df, k, 0, open=99, high=99.4, low=98.8, close=99.2)
    setbar(df, k, 1, open=99.3, high=99.5, low=99.1, close=99.4)
    setbar(df, k, 75, open=100, high=100.2, low=99.9, close=100)
    v = validate(df, "KRX_ETF")
    assert build_trades(v)["gap_down_0.75pct"] == []
    full = build_trades(v, fee_bps=0, slippage_bps=0,
                        corporate_actions_reviewed=True)
    day = v.iloc[k*76]["session"]
    t = next(x for x in full["gap_down_0.75pct"] if x["session"] == day)
    assert t["entry_open"] == 99.3
    assert t["net_return"] == pytest.approx(100 / 99.3 - 1)
    assert not any(x["session"] == day for x in full["gap_down_1.50pct"])


def test_momentum_only_after_first_30min_and_sells_last_bar_open():
    df = bars()
    k = 35
    setbar(df, k, 5, high=101, close=100.5)
    setbar(df, k, 6, open=101, high=101.1, low=100.8, close=101)
    setbar(df, k, 75, open=102, high=102.1, low=101.9, close=102)
    v = validate(df, "KRX_ETF")
    got = build_trades(v, fee_bps=0, slippage_bps=0)["intraday_momentum_30m"]
    t = next(x for x in got if x["session"] == v.iloc[k*76]["session"])
    assert t["entry_open"] == 101
    assert t["exit_open"] == 102
    assert t["net_return"] == pytest.approx(102/101 - 1)


def test_costs_only_reduce_net_and_test_changes_never_reselect():
    df = bars()
    # Signals on 30 sessions: 5min opening rvol high for some, momentum all.
    for k in range(15,100):
        setbar(df, k, 5, high=100.5, close=100.4)
        setbar(df, k, 6, open=100.6, high=100.7, low=100.5, close=100.6)
        setbar(df, k, 75, open=101, high=101.1, low=100.9, close=101)
    v = validate(df, "KRX_ETF")
    gross = build_trades(v, fee_bps=0, slippage_bps=0)
    net = build_trades(v, fee_bps=3, slippage_bps=5, sell_tax_bps=20)
    t0 = gross["intraday_momentum_30m"][0]
    t1 = net["intraday_momentum_30m"][0]
    assert t1["net_return"] < t0["net_return"]
    original = evaluate(v, market="KRX_ETF", source_sha256=SHA)
    changed = df.copy()
    for k in range(80,100):
        setbar(changed, k, 75, open=120, high=120.1, low=119.9, close=120)
    v2 = validate(changed, "KRX_ETF")
    later = evaluate(v2, market="KRX_ETF", source_sha256=SHA)
    assert original["selected_by_validation_only"] == later["selected_by_validation_only"]
    assert (original["untouched_test_selected_only"]["intraday_momentum"]
            ["net_compounded_return"] !=
            later["untouched_test_selected_only"]["intraday_momentum"]
            ["net_compounded_return"])


def test_cli_report_never_touches_operating_state(tmp_path):
    from quantlab.intraday_research import main
    csv = tmp_path/"in.csv"
    out = tmp_path/"isolated"/"report.json"
    bars().to_csv(csv, index=False)
    assert main(["--csv", str(csv), "--market", "US_EQUITY",
                 "--out", str(out)]) == 2  # US time zone mismatch is rejected
    assert not out.exists()
    assert main(["--csv", str(csv), "--market", "KRX_ETF",
                 "--out", str(out)]) == 0
    import json
    report = json.loads(out.read_text())
    assert report["market"] == "KRX_ETF"
    assert report["status"] == "ISOLATED_INTRADAY_RESEARCH_ONLY_NOT_PAPER_ELIGIBLE"
    assert not (tmp_path/"paper.db").exists()


def test_krx_closing_auction_not_continuous_fill():
    from quantlab.intraday_research import MARKETS
    assert MARKETS["KRX_STOCK"][3] == 76
    assert MARKETS["KRX_ETF"][3] == 76
    assert MARKETS["US_EQUITY"][3] == 78
    df = bars()
    assert df.iloc[75]["timestamp"].endswith("15:15:00+09:00")
    assert df.iloc[76]["timestamp"].endswith("09:00:00+09:00")
    v = validate(df, "KRX_ETF")
    assert v.iloc[75]["session"] != v.iloc[76]["session"]
    suffix = df.iloc[75].copy()
    suffix["timestamp"] = suffix["timestamp"].replace("15:15:", "15:20:")
    bad = pd.concat([df.iloc[:76], pd.DataFrame([suffix]), df.iloc[76:]], ignore_index=True)
    with pytest.raises(IntradayDataError, match="Incomplete"):
        validate(bad, "KRX_ETF")

    report = evaluate(v, market="KRX_ETF", source_sha256=SHA,
                      corporate_actions_reviewed=True)
    assert report["krx_closing_auction_not_modeled"]
    assert report["gap_strategy_blocked_without_review"]
    assert report["selected_by_validation_only"]["gap_reversion"] is None
    assert report["last_continuous_bar_start_local"] == "15:15"

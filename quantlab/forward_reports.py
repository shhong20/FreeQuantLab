"""Independent daily performance reports from real forward virtual observations.

Historical OHLCV is NEVER used to fabricate fills/marks, and no yearly Sharpe
or projected return is produced from a handful of untested daily observations.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from .monitor import snapshot, _read_db, KST


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2%}"


def report(db: str | Path, *, now: datetime | None = None) -> dict:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Report time must be timezone-aware")
    instant = instant.astimezone(timezone.utc)
    snap = snapshot(db, now=instant)
    with _read_db(db) as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT e.track_id,e.decision_date,e.session_date,e.action,"
            "e.simulated_cost,e.equity FROM shadow_events e "
            "ORDER BY e.track_id,e.decision_date"
        ).fetchall()
        conn.execute("COMMIT")
    events: dict[str,list] = {}
    for row in rows:
        events.setdefault(row["track_id"], []).append(row)
    entries = []
    for track in snap["tracks"]:
        history = events.get(track["id"], [])
        previous_date = date.fromisoformat(track["frozen_bar"])
        skipped = 0
        profitable = 0
        last_equity = track["initial_virtual_cash"]
        largest_observed_loss = None
        costs = 0.
        for event in history:
            day = date.fromisoformat(event["decision_date"])
            gap = (day - previous_date).days - 1
            skipped += max(0, gap)
            previous_date = day
            equity = float(event["equity"])
            observed_return = equity / last_equity - 1.
            if observed_return > 0:
                profitable += 1
            largest_observed_loss = (
                observed_return if largest_observed_loss is None else
                min(largest_observed_loss, observed_return)
            )
            costs += float(event["simulated_cost"])
            last_equity = equity
        if history:
            expected_first = date.fromisoformat(track["frozen_bar"])
            assert date.fromisoformat(history[0]["decision_date"]) > expected_first
        entries.append({
            "track_id": track["id"],
            "symbol": track["symbol"],
            "strategy": track["strategy"],
            "state": track["state"],
            "first_observed_date": history[0]["decision_date"] if history else None,
            "last_observed_date": history[-1]["decision_date"] if history else None,
            "recorded_days": len(history),
            "missing_intermediate_utc_days": skipped,
            "simulated_orders": track["simulated_orders"],
            "recorded_equity": track["recorded_equity"],
            "observed_total_return": track["observed_return"],
            "observed_max_drawdown": track["max_recorded_drawdown"],
            "best_excluded": True,  # explicit: strategy choice never uses this report
            "positive_observation_days": profitable if history else None,
            "worst_observed_interval_return": largest_observed_loss,
            "simulated_cumulative_cost": round(costs, 6),
            "halted": track["halted"],
        })
    observed_dates_by_track = {
        track_id: tuple(e["decision_date"] for e in stream)
        for track_id, stream in events.items()
    }
    hold_by_symbol = {
        t["symbol"]: t for t in entries if t["strategy"] == "buy_hold_20pct"
    }
    for t in entries:
        reference = hold_by_symbol.get(t["symbol"])
        comparable = (
            reference is not None and t["recorded_days"] > 0
            and t["first_observed_date"] == reference["first_observed_date"]
            and t["last_observed_date"] == reference["last_observed_date"]
            and t["recorded_days"] == reference["recorded_days"]
            and observed_dates_by_track.get(t["track_id"], ())
                == observed_dates_by_track.get(reference["track_id"], ())
        )
        t["vs_buy_hold_return_pp"] = (
            round(100*(t["observed_total_return"] -
                       reference["observed_total_return"]), 6)
            if comparable else None
        )
        t["comparison_observations_aligned"] = comparable
    return {
        "report_date_kst": instant.astimezone(KST).date().isoformat(),
        "generated_at_utc": instant.isoformat(),
        "kind": "forward_observation_based_shadow_paper_daily_summary",
        "real_orders_enabled": False,
        "strategy_auto_promotion_enabled": False,
        "summary": snap["summary"],
        "alerts": snap["alerts"],
        "strategies": entries,
        "limitations": [
            "Only truly recorded forward virtual observations; no backfilled historical trades.",
            "A missing quote day is not filled with a made-up equity price.",
            "Daily snapshots do not prove tradable fills or real-account profitability.",
            "Individual independent virtual accounts cannot be pooled or their returns summed.",
            "Return differences are descriptive percentage points, not alpha significance.",
            "No annualized Sharpe or model selection from this immature forward sample.",
        ],
    }


def to_markdown(item: dict) -> str:
    lines = [
        f"# FreeQuantLab 선행 모의투자 일일 보고서 — {item['report_date_kst']}",
        "",
        "> **가상 투자 관찰 전용** · 실제 거래 주문 0건 · 전략 자동 승격 불가",
        "",
        f"- 작성 시각(UTC): {item['generated_at_utc']}",
        f"- 등록 전략: {item['summary']['tracks']}개",
        f"- 누적 미래 관측 이벤트: {item['summary']['recorded_events']}건",
        f"- 가상 주문(BUY/SELL): {item['summary']['virtual_orders']}건",
        f"- 위험 경고: {item['summary']['critical_alerts']}건 / 운영 주의: {item['summary']['warnings']}건",
        "",
        "## 전략별 실제 선행 관찰",
        "",
        "| 자산 | 전략 | 관측 일수 | 누적 가상 수익률 | 관측 MDD | 매수·보유 대비 차이(pp) | 누락 UTC일 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for t in item["strategies"]:
        rel = ("—" if t["vs_buy_hold_return_pp"] is None
               else f"{t['vs_buy_hold_return_pp']:+.2f} pp")
        lines.append(
            f"| {t['symbol']} | {t['strategy']} | {t['recorded_days']} | "
            f"{_percent(t['observed_total_return'])} | "
            f"{_percent(t['observed_max_drawdown'])} | {rel} | "
            f"{t['missing_intermediate_utc_days']} |"
        )
    lines += [
        "", "## 운영 경고", "",
    ]
    if item["alerts"]:
        for a in item["alerts"]:
            lines.append(
                f"- {a['severity'].upper()} / {a['code']}: "
                f"{a['message']} ({a.get('symbol', '-')} {a.get('strategy', '-')})"
            )
    else:
        lines.append("- 현재 원장 관측 기준 활성 경고 없음")
    lines += ["", "## 해석 주의", ""]
    lines += [f"- {x}" for x in item["limitations"]]
    lines.append("")
    return "\n".join(lines)


def _atomic(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".forward-report-",
                                         delete=False) as tmp:
            name = tmp.name
            os.fchmod(tmp.fileno(), 0o600)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def save_report(payload: dict, target_dir: str | Path) -> tuple[Path,Path]:
    day = payload["report_date_kst"]
    if not isinstance(day, str) or len(day) != 10 or date.fromisoformat(day).isoformat() != day:
        raise ValueError("Invalid report date")
    base = Path(target_dir)
    json_path, md_path = base / f"{day}.json", base / f"{day}.md"
    _atomic(json_path, (json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                   indent=2)+"\n").encode("utf-8"))
    _atomic(md_path, to_markdown(payload).encode("utf-8"))
    return json_path, md_path

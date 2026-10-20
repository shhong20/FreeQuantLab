"""Shadow forward-ledger invariants (offline, public prices mocked)."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from quantlab import cli
from quantlab.data import demo_daily
from quantlab.settings import Settings
from quantlab.shadow import bootstrap, run_cycle, status, _spec, _target, _connection


ASSETS = [
    {"provider": "upbit", "symbol": "KRW-BTC", "kind": "crypto", "currency": "KRW", "bars": 320},
    {"provider": "upbit", "symbol": "KRW-ETH", "kind": "crypto", "currency": "KRW", "bars": 320},
]
T20 = datetime(2026, 10, 20, 2, 30, tzinfo=timezone.utc)
T21 = datetime(2026, 10, 21, 0, 14, tzinfo=timezone.utc)
T22 = datetime(2026, 10, 22, 0, 14, tzinfo=timezone.utc)


def frame(end: str, *, n: int = 320):
    bars = demo_daily(n)
    bars["date"] = pd.date_range(end=end, periods=n)
    return bars


def run(fake_db, data, now=T21, price=100.):
    calls = []
    def quote(provider, symbol, *, now):
        calls.append(symbol)
        return price, now.date().isoformat()
    result = run_cycle(fake_db, ASSETS, now=now,
                       fetch_fn=lambda a: data[a["symbol"]], quote_fn=quote)
    return result, calls


def test_freeze_no_backfilled_virtual_trades(tmp_path):
    db = tmp_path / "shadow.db"
    old = {"KRW-BTC": frame("2026-10-19"), "KRW-ETH": frame("2026-10-19")}
    registered = bootstrap(db, ASSETS, Settings(), now=T20,
                           fetch_fn=lambda a: old[a["symbol"]])
    assert registered["created_tracks"] == 6
    assert registered["real_orders_enabled"] is False
    assert bootstrap(db, ASSETS, Settings(), now=T20,
                     fetch_fn=lambda a: old[a["symbol"]])["created_tracks"] == 0
    summary = status(db)
    assert len(summary["tracks"]) == 6
    assert all(t["events"] == 0 and t["last_decision_date"] is None for t in summary["tracks"])
    no_new, quotes = run(db, old, now=T20)
    assert not quotes
    assert no_new["status"] == "OK"
    assert not any(t["events"] for t in status(db)["tracks"])


def test_first_genuinely_new_closed_bar_six_accounts_and_idempotency(tmp_path):
    db = tmp_path / "shadow.db"
    old = {"KRW-BTC": frame("2026-10-19"), "KRW-ETH": frame("2026-10-19")}
    bootstrap(db, ASSETS, Settings(), now=T20, fetch_fn=lambda a: old[a["symbol"]])
    new = {"KRW-BTC": frame("2026-10-20"), "KRW-ETH": frame("2026-10-20")}
    first, quotes = run(db, new)
    assert first["status"] == "OK"
    assert sorted(quotes) == ["KRW-BTC", "KRW-ETH"]
    assert sum(r["status"] == "RECORDED" for r in first["results"]) == 6
    stat = status(db)
    assert all(t["events"] == 1 for t in stat["tracks"])
    assert all(t["last_decision_date"] == "2026-10-20" for t in stat["tracks"])
    assert stat["real_orders_enabled"] is False
    second, quotes2 = run(db, new)
    assert second["status"] == "OK"
    assert quotes2 == []
    assert all(t["events"] == 1 for t in status(db)["tracks"])


def test_virtual_track_cash_separate_and_cost_recorded(tmp_path):
    db = tmp_path / "shadow.db"
    btc = [ASSETS[0]]
    bootstrap(db, btc, Settings(), now=T20, fetch_fn=lambda a: frame("2026-10-19"))
    def quote(provider, symbol, *, now):
        return 100, now.date().isoformat()
    run_cycle(db, btc, now=T21, fetch_fn=lambda a: frame("2026-10-20"), quote_fn=quote)
    summary = status(db)
    assert len(summary["tracks"]) == 3
    assert all(t["cash"] >= 0 and t["last_recorded_equity"] <= 1e6 for t in summary["tracks"])
    assert all(t["eligible_for_promotion"] is False for t in summary["tracks"])
    with _connection(db) as con:
        rows = con.execute("SELECT action,quantity,simulated_cost FROM shadow_events").fetchall()
        assert len(rows) == 3
        assert any(r["action"] == "BUY" and r["quantity"] > 0 and r["simulated_cost"] > 0 for r in rows)


def test_daily_loss_limit_persists_halt_and_never_auto_unhalts(tmp_path):
    db = tmp_path / "shadow.db"
    btc = [ASSETS[0]]
    bootstrap(db, btc, Settings(), now=T20, fetch_fn=lambda a: frame("2026-10-19"))
    run_cycle(db, btc, now=T21, fetch_fn=lambda a: frame("2026-10-20"),
              quote_fn=lambda p,s,*,now:(100.,now.date().isoformat()))
    result = run_cycle(db, btc, now=T22, fetch_fn=lambda a: frame("2026-10-21"),
              quote_fn=lambda p,s,*,now:(45.,now.date().isoformat()))
    assert result["status"] == "OK"
    stat = status(db)
    assert any(t["halted"] for t in stat["tracks"])
    assert all(t["events"] == 2 for t in stat["tracks"])
    halt_events = [r for r in result["results"] if r.get("action") == "HALT"]
    assert halt_events
    third = run_cycle(db, btc, now=datetime(2026,10,23,0,14,tzinfo=timezone.utc),
                      fetch_fn=lambda a: frame("2026-10-22"),
                      quote_fn=lambda p,s,*,now:(40.,now.date().isoformat()))
    assert third["status"] == "OK"
    assert any(t["halted"] for t in status(db)["tracks"])


def test_shadow_config_drift_rejected_and_preserves_original(tmp_path):
    db = tmp_path / "shadow.db"
    btc = [ASSETS[0]]
    bootstrap(db, btc, Settings(), now=T20, fetch_fn=lambda a: frame("2026-10-19"))
    with pytest.raises(ValueError, match="changed"):
        bootstrap(db, btc, Settings(max_position_fraction=.11), now=T20,
                  fetch_fn=lambda a: frame("2026-10-19"))
    assert len(status(db)["tracks"]) == 3


def test_stale_quotes_cannot_create_virtual_fills(tmp_path):
    db = tmp_path / "shadow.db"
    btc = [ASSETS[0]]
    bootstrap(db, btc, Settings(), now=T20, fetch_fn=lambda a: frame("2026-10-19"))
    res = run_cycle(db, btc, now=T21, fetch_fn=lambda a: frame("2026-10-20"),
                    quote_fn=lambda p,s,*,now:(100.,"2026-10-19"))
    assert res["status"] == "ERROR"
    assert all(t["events"] == 0 for t in status(db)["tracks"])
    assert status(db)["runs"][0]["status"] == "ERROR"


def test_reject_unsupported_providers_malformed_schemas_and_old_bars(tmp_path):
    with pytest.raises(ValueError):
        _spec({"kind":"factor","label":"x","feature":"__class__","direction":"gt","threshold":0})
    with pytest.raises(ValueError):
        _spec({"kind":"factor","label":"x","feature":"r5","direction":"gt","threshold":float("nan")})
    with pytest.raises(ValueError):
        _spec({"kind":"factor","label":"x","feature":"r5","direction":"gt","threshold":True})
    with pytest.raises(ValueError):
        _spec({"kind":"momentum","label":"x","fast":80,"slow":20})
    with pytest.raises(ValueError):
        bootstrap(tmp_path/"s.db", [{"provider":"demo","symbol":"DEMO","currency":"TEST"}],
                  Settings(), now=T20, fetch_fn=lambda a: frame("2026-10-19"))
    with pytest.raises(ValueError, match="yesterday"):
        bootstrap(tmp_path/"s.db", [ASSETS[0]], Settings(), now=T20,
                  fetch_fn=lambda a: frame("2026-10-18"))


def test_strategy_target_only_uses_completed_history():
    df = frame("2026-10-20")
    raw = {"kind":"factor","label":"x","feature":"r5","direction":"gt","threshold":0}
    fast = {"kind":"momentum","label":"y","fast":20,"slow":80}
    a, b = _target(df,raw), _target(df,fast)
    changed = df.copy()
    changed.loc[len(df)-1, "open"] *= 3
    # Signals depend only on completed close, no next-session quote.
    assert _target(changed,raw) == a
    assert _target(changed,fast) == b


def test_cli_status_is_separate_and_no_legacy_mutation(tmp_path):
    db = tmp_path / "incubation.db"
    assert cli.main(["--db",str(db),"shadow-status"]) == 0
    assert not db.exists()
    assert cli.main(["shadow-cycle"]) == 2  # rejects legacy paper.db


def test_database_persistence_after_reopen_and_ledger_unique(tmp_path):
    db = tmp_path / "shadow.db"
    btc = [ASSETS[0]]
    bootstrap(db, btc, Settings(), now=T20, fetch_fn=lambda a: frame("2026-10-19"))
    run_cycle(db, btc, now=T21, fetch_fn=lambda a: frame("2026-10-20"),
              quote_fn=lambda p,s,*,now:(100.,now.date().isoformat()))
    with _connection(db) as con:
        with pytest.raises(Exception):
            con.execute("""INSERT INTO shadow_events
                SELECT track_id, decision_date, session_date, action, target,
                       observed_price, quantity, simulated_cost, cash, units,
                       equity, created_at FROM shadow_events LIMIT 1""")
    assert len(status(db)["tracks"]) == 3


def test_recovered_service_does_not_backfill_missed_dates(tmp_path):
    db = tmp_path / "shadow.db"
    btc = [ASSETS[0]]
    bootstrap(db, btc, Settings(), now=T20, fetch_fn=lambda a: frame("2026-10-19"))
    run_cycle(db, btc, now=T21, fetch_fn=lambda a: frame("2026-10-20"),
              quote_fn=lambda p,s,*,now:(100.,now.date().isoformat()))
    t23 = datetime(2026, 10, 23, 0, 14, tzinfo=timezone.utc)
    result = run_cycle(db, btc, now=t23, fetch_fn=lambda a: frame("2026-10-22"),
                       quote_fn=lambda p,s,*,now:(101.,now.date().isoformat()))
    recorded = [r for r in result["results"] if r.get("status") == "RECORDED"]
    assert len(recorded) == 3
    assert all(r["unobserved_utc_days"] == 1 for r in recorded)
    with _connection(db) as con:
        dates = {r[0] for r in con.execute("SELECT DISTINCT decision_date FROM shadow_events")}
    assert dates == {"2026-10-20", "2026-10-22"}
    assert all(t["events"] == 2 for t in status(db)["tracks"])


def test_cycle_lock_is_nonblocking(tmp_path):
    import fcntl
    db = tmp_path / "shadow.db"
    btc = [ASSETS[0]]
    bootstrap(db, btc, Settings(), now=T20, fetch_fn=lambda a: frame("2026-10-19"))
    lock = str(db) + ".lock"
    with open(lock, "a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            run_cycle(db, btc, now=T20, fetch_fn=lambda a: frame("2026-10-19"),
                      quote_fn=lambda p,s,*,now:(100.,now.date().isoformat()))
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

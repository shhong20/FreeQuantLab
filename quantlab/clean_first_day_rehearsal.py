"""Rehearse tomorrow's first clean 90d *synthetically*, with a disposable SQLite clone.

Uses exact immutable account identities, settings, and registration manifest from
the existing real pre-registered ledger. All market candles and public ticker
quotes are SYNTHETIC. The source database is opened with SQLite mode=ro and
only backed up into a Python-managed TemporaryDirectory; the cloned account
events and synthetic fills are irreversibly discarded after the rehearsal.

This is not evidence that tomorrow's real market/Upbit network will succeed.
No broker call, future live information, historical backfill or PnL published.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pandas as pd

from .clean_cohort import inspect_clean
from .data import demo_daily
from .prospective import read_protocol, _utc
from .protocol_compliance import verify_frozen_accounts
from .shadow import run_cycle


class RehearsalError(RuntimeError):
    pass


def _immutable_source_status(source: Path, manifest: Path, *, now:datetime) -> dict:
    plan=read_protocol(manifest)
    from .monitor import _read_db
    from .ledger_audit import audit_shadow_ledger
    from .quote_quality import execution_proxy_quality
    if audit_shadow_ledger(source,now=now)["status"]!="PASS":
        raise RehearsalError("Source audit failed. Refuse scenario with invalid ledger")
    if execution_proxy_quality(source)["status"]!="AWAITING_FIRST_OBSERVATION":
        raise RehearsalError("Rehearsal requires untouched first-day baseline")
    with _read_db(source) as conn:
        rows=conn.execute("SELECT * FROM shadow_accounts").fetchall()
        events=conn.execute("SELECT count(*) FROM shadow_events").fetchone()[0]
        quotes=conn.execute("SELECT count(*) FROM shadow_quote_evidence").fetchone()[0]
        state={str(a["track_id"]):(a["cash"],a["units"],a["halted"]) for a in rows}
    verify_frozen_accounts(rows,manifest,now=now)
    if events or quotes or len(rows)!=6:
        raise RehearsalError("Source is not the empty, 6-account 90d preregistration")
    if any(float(cash)!=1_000_000 or float(units)!=0 or halted
           for cash,units,halted in state.values()):
        raise RehearsalError("First-day opening inventory no longer flat")
    return {"sha":plan["sha256"],"accounts":sorted(state.items()),
            "events":events,"quotes":quotes}


def rehearsal(source_db:str|Path, protocol:str|Path, *,
              now:datetime|None=None) -> dict:
    """Run the full six-account first-day engine on transient fake market data."""
    current=_utc(now)
    source=Path(source_db).resolve(strict=True)
    manifest=Path(protocol).resolve(strict=True)
    opening=_immutable_source_status(source,manifest,now=current)
    plan=read_protocol(manifest)
    first=date.fromisoformat(plan["first_eligible_decision_utc"])
    frozen_start=datetime.fromisoformat(plan["created_at_utc"]).astimezone(timezone.utc)
    if current<frozen_start:
        raise RehearsalError("Clock precedes protocol registration")
    if current.date()>first:
        # Replaying yesterday's synthetic exercise after experiment has started
        # would be misleading. Use a fresh test fixture instead.
        raise RehearsalError("Pre-first-day rehearsal is only valid before first decision closes")
    planned=datetime.combine(first+timedelta(days=1),
                             time(0,24),timezone.utc)
    due=datetime.combine(first+timedelta(days=1),
                         time(1,21),timezone.utc)
    no_window=datetime.combine(first+timedelta(days=1),
                               time(5,0),timezone.utc)
    missing=inspect_clean(source,manifest,now=due)
    if missing["status"]!="INCOMPLETE" or missing["expected_due_account_days"]!=6:
        raise RehearsalError("Skipped-day watchdog rehearsal did not detect six due accounts")

    assets=[{"provider":"upbit","symbol":symbol,"kind":"crypto",
             "currency":"KRW","bars":320}
            for symbol in ("KRW-BTC","KRW-ETH")]
    fake=demo_daily(320)
    fake["date"]=pd.date_range(end=first.isoformat(),periods=320)
    # Label everything synthetic. Do not request or reuse tomorrow's Upbit
    # market candles or actual public ticker prices.
    cloned_quotes=[]
    def fake_fetch(asset):
        return fake.copy()
    def fake_quote(provider,symbol,*,now):
        assert (provider,symbol) in {("upbit","KRW-BTC"),("upbit","KRW-ETH")}
        assert now==planned
        cloned_quotes.append(symbol)
        return float(fake["close"].iloc[-1]),now.date().isoformat()

    with TemporaryDirectory(prefix="freequant-clean-rehearsal-") as folder:
        target=Path(folder)/"synthetic_only.sqlite3"
        with sqlite3.connect("file:"+str(source)+"?mode=ro",uri=True,timeout=15) as src:
            with sqlite3.connect(target,timeout=15) as dst:
                src.backup(dst,pages=64)
        initial=_immutable_source_status(target,manifest,now=current)
        if initial!=opening:
            raise RehearsalError("SQLite snapshot changed first-day identities")
        first_run=run_cycle(target,assets,now=planned,fetch_fn=fake_fetch,
                            quote_fn=fake_quote,protocol_path=manifest)
        if first_run["status"]!="OK" or first_run["errors"]:
            raise RehearsalError("First synthetic 6-account run failed: "+
                                  str(first_run["errors"])[:220])
        result=inspect_clean(target,manifest,now=due)
        if (result["status"]!="PASS" or
            result["recorded_due_account_days"]!=6 or
            result["invalid_due_account_days"] or
            result["quote_proxy_status"]!="PASS" or
            result["ledger_audit_status"]!="PASS"):
            raise RehearsalError("First due independent forensic checks failed")
        with sqlite3.connect("file:"+str(target)+"?mode=ro",uri=True) as c:
            events_before=c.execute("SELECT count(*) FROM shadow_events").fetchone()[0]
            quote_before=c.execute("SELECT count(*) FROM shadow_quote_evidence").fetchone()[0]
            orders=c.execute("SELECT count(*) FROM shadow_events WHERE action IN ('BUY','SELL')").fetchone()[0]
        if events_before!=6 or quote_before!=2 or sorted(cloned_quotes)!=["KRW-BTC","KRW-ETH"]:
            raise RehearsalError("Atomic complete first day was not generated in disposable clone")
        again=run_cycle(target,assets,now=planned,fetch_fn=fake_fetch,
                        quote_fn=lambda *a,**k: (_ for _ in ()).throw(
                            RehearsalError("Duplicate must not request market ticker")),
                        protocol_path=manifest)
        late=run_cycle(target,assets,now=no_window,
                       fetch_fn=lambda *a,**k: (_ for _ in ()).throw(
                           RehearsalError("Late replay must not fetch bars")),
                       quote_fn=lambda *a,**k: (_ for _ in ()).throw(
                           RehearsalError("Late replay must not fetch public tick")),
                       protocol_path=manifest)
        if again["status"]!="OK" or late["results"][0]["status"]!="OUTSIDE_OBSERVATION_WINDOW":
            raise RehearsalError("Duplicate/late restart guard failed")
        with sqlite3.connect(target) as c:
            if (c.execute("SELECT count(*) FROM shadow_events").fetchone()[0]!=events_before
                or c.execute("SELECT count(*) FROM shadow_quote_evidence").fetchone()[0]!=quote_before):
                raise RehearsalError("Duplicate or late replay altered immutable first-day facts")
            c.execute("UPDATE shadow_quote_evidence SET public_ticker_price=1 WHERE symbol='KRW-ETH'")
        corrupted=inspect_clean(target,manifest,now=due)
        if corrupted["status"]!="INCOMPLETE" or not corrupted["issues"]:
            raise RehearsalError("Quote tamper guard did not reject altered synthetic evidence")

    # Re-check the REAL pre-registered source after disposable clone vanishes.
    unchanged=_immutable_source_status(source,manifest,now=current)
    if unchanged!=opening:
        raise RehearsalError("Real clean cohort changed while exercising disposable synthetic clone")
    return {
        "status":"SYNTHETIC_FIRST_DAY_REHEARSAL_PASS_NOT_REAL_MARKET",
        "protocol_sha256":opening["sha"],
        "registered_real_accounts":len(opening["accounts"]),
        "synthetic_target_decision_utc":first.isoformat(),
        "synthetic_run_utc":planned.isoformat(),
        "synthetic_completed_account_days":events_before,
        "synthetic_public_quote_rows":quote_before,
        "synthetic_virtual_order_count_not_performance":orders,
        "synthetic_independent_audit":"PASS",
        "skip_day_alert_when_missing":"INCOMPLETE",
        "synthetic_modified_quote_rejected":True,
        "repeated_run_duplicate_events":0,
        "late_restart_generated_events":0,
        "real_source_events_before_and_after":0,
        "source_database_unchanged":True,
        "discarded_synthetic_database":True,
        "historical_backfill_performed":False,
        "real_broker_orders":0,
        "automatic_strategy_promotion":False,
        "live_network_queried":False,
        "stock_or_crypto_90d_evaluation_started_by_rehearsal":False,
    }


def main(argv=None)->int:
    parser=argparse.ArgumentParser(
        description="Disposable synthetic first-day clean paper rehearsal. No trading.")
    parser.add_argument("--db",default="/home/hong/apps/FreeQuantLab/state/clean_shadow_90d.db")
    parser.add_argument("--protocol",default=(
        "/home/hong/apps/FreeQuantLab-clean-runtime/protocols/"
        "2026-10-21-btc-eth-clean-90d.json"))
    args=parser.parse_args(argv)
    try:
        print(json.dumps(rehearsal(args.db,args.protocol),
                         sort_keys=True,ensure_ascii=False))
        return 0
    except (RehearsalError,ValueError,KeyError,sqlite3.Error,OSError) as exc:
        print(json.dumps({"status":"SYNTHETIC_REHEARSAL_FAILED_CLOSED",
                          "error_type":type(exc).__name__,
                          "errors_in_live_market_not_inferred":True}),
              flush=True)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

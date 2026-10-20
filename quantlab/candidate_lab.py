"""Single-shot research-Agent factor freeze to an ISOLATED virtual account.

Local constrained model proposes a *declarative* factor. We take its FIRST
allowlisted proposal without searching recent OOS for a better one, then
seal immutable plan. No model-created source code or runtime LLM calls in
the daily paper engine. This is NOT Qlib or RD-Agent and not live trading.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from typing import Callable

import pandas as pd

from .agent_lab import FactorProposal, LocalLLMAgent, _summarize_training
from .data import fetch
from .monitor import _read_db
from .prospective import _canonical
from .quality import validate_shadow_market
from .settings import Settings
from .shadow import _account_id, _spec, bootstrap, run_cycle


class CandidateLabError(ValueError):
    pass


def _utc(now: datetime | None) -> datetime:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise CandidateLabError("Aware UTC timestamp required")
    return instant.astimezone(timezone.utc)


def _guard_isolated(target_db: str | Path, source_db: str | Path = "state/shadow.db"):
    if Path(target_db).resolve()==Path(source_db).resolve():
        raise CandidateLabError("Frozen production shadow ledger cannot be used by Agent Lab")
    if Path(target_db).resolve().name not in {"agent_lab_shadow.db"}:
        raise CandidateLabError("Agent Lab is restricted to agent_lab_shadow.db")


def propose_lab(
    path: str | Path, *, assets: list[dict], settings: Settings,
    agent: LocalLLMAgent,
    now: datetime | None = None, fetch_fn: Callable = fetch,
) -> dict:
    instant=_utc(now)
    if not isinstance(agent, LocalLLMAgent):
        raise CandidateLabError("Only actual configured local LLM, never mock, for real frozen plan")
    if not assets or len({a.get("symbol") for a in assets})!=len(assets):
        raise CandidateLabError("Nonempty unique assets required")
    entries=[]
    for a in assets:
        if (a.get("provider")!="upbit" or a.get("currency")!="KRW"
             or a.get("kind")!="crypto" or not a.get("symbol","").startswith("KRW-")):
            raise CandidateLabError("Only validated Upbit KRW spot may enter this lab")
        candles=fetch_fn(a)
        quality=validate_shadow_market(candles,instant)
        frame=candles.reset_index(drop=True)
        summary=_summarize_training(frame,len(frame))
        proposed=agent.propose(summary)
        # No validation/test-driven proposal shopping. First candidate only.
        choice=proposed[0]
        factor=FactorProposal.parse({
            "name":"agent_factor_1",
            "feature":choice.feature,
            "direction":choice.direction,
            "threshold":choice.threshold,
        })
        spec=_spec({
            "label":"agent_factor_1","kind":"factor",
            "feature":factor.feature,"direction":factor.direction,
            "threshold":factor.threshold,
        })
        entries.append({
            "asset":a,"spec":spec,"track_id":_account_id(a,spec,settings),
            "frozen_utc_candle":quality["completed_utc_day"],
            "training_candle_count":len(frame),
            "last_training_close":float(frame["close"].iloc[-1]),
            "lookahead_labels_used":False,
            "proposals_evaluated_on_oos":0,
        })
    body={
        "schema_version":1,
        "research_lane":"isolated_local_llm_factor_only",
        "model_id":agent.model,
        "created_utc":instant.isoformat(),
        "source":"local_model_constrained_json_training_summary_only",
        "strategy_selection":"first_allowed_factor_no_oos_search",
        "account_config":__import__("dataclasses").asdict(settings),
        "entries":entries,
        "isolation_db_basename":"agent_lab_shadow.db",
        "automatic_runner_enabled":False,
        "qlib_model_execution_enabled":False,
        "broker_orders_enabled":False,
        "eligible_for_promotion":False,
        "agent_generated_code_executed":False,
    }
    body["sha256"]=sha256(_canonical(body)).hexdigest()
    dst=Path(path)
    dst.parent.mkdir(parents=True,exist_ok=True)
    try:
        with dst.open("xb") as handle:
            os.fchmod(handle.fileno(),0o600)
            handle.write(json.dumps(body,ensure_ascii=False,indent=2).encode("utf-8")+b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise CandidateLabError("Candidate plan already frozen. Never overwrite.") from exc
    return body


def read_lab_plan(path: str | Path) -> dict:
    obj=json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(obj,dict) or obj.get("schema_version")!=1:
        raise CandidateLabError("Invalid lab schema")
    digest=obj.get("sha256")
    if sha256(_canonical({k:v for k,v in obj.items() if k!="sha256"})).hexdigest()!=digest:
        raise CandidateLabError("Lab plan content hash changed")
    if (obj.get("research_lane")!="isolated_local_llm_factor_only" or
        obj.get("isolation_db_basename")!="agent_lab_shadow.db" or
        obj.get("automatic_runner_enabled") is not False or
        obj.get("qlib_model_execution_enabled") is not False or
        obj.get("broker_orders_enabled") is not False or
        obj.get("eligible_for_promotion") is not False):
        raise CandidateLabError("Unsafe lab permissions")
    if not obj["entries"] or len({x["asset"]["symbol"] for x in obj["entries"]})!=len(obj["entries"]):
        raise CandidateLabError("Invalid lab universe")
    for entry in obj["entries"]:
        if _spec(entry["spec"]) != entry["spec"]:
            raise CandidateLabError("Invalid frozen factor")
        if entry["spec"]["kind"]!="factor" or entry["spec"]["label"]!="agent_factor_1":
            raise CandidateLabError("Unsafe laboratory strategy kind")
        cfg=Settings(**obj["account_config"])
        if _account_id(entry["asset"],entry["spec"],cfg)!=entry["track_id"]:
            raise CandidateLabError("Frozen strategy identity inconsistent")
    return obj


def _verify_registered(db: str | Path, obj: dict):
    with _read_db(db) as con:
        accounts=con.execute("SELECT * FROM shadow_accounts").fetchall()
    expected={x["track_id"]:x for x in obj["entries"]}
    if len(accounts)!=len(expected) or {a["track_id"] for a in accounts}!=set(expected):
        raise CandidateLabError("Lab account universe changed")
    for a in accounts:
        item=expected[a["track_id"]]
        if a["symbol"]!=item["asset"]["symbol"] or json.loads(a["spec_json"])!=item["spec"]:
            raise CandidateLabError("Lab frozen factor changed")
        if json.loads(a["settings_json"])!=obj["account_config"]:
            raise CandidateLabError("Lab frozen settings changed")
        if a["frozen_bar"]!=item["frozen_utc_candle"]:
            raise CandidateLabError("Lab registration date changed")


def register_lab(
    plan: str | Path, db: str | Path, *,
    now: datetime | None = None, fetch_fn: Callable = fetch,
    production_db: str | Path = "state/shadow.db",
) -> dict:
    _guard_isolated(db,production_db)
    current=_utc(now)
    obj=read_lab_plan(plan)
    frozen_day=datetime.fromisoformat(obj["created_utc"]).date()
    if current.date()!=frozen_day or current<datetime.fromisoformat(obj["created_utc"]):
        raise CandidateLabError("Registration must be same UTC date after plan lock")
    if Path(db).exists():
        raise CandidateLabError("Lab ledger already exists; do not overwrite or re-register")
    grouped={}
    cfg=Settings(**obj["account_config"])
    for row in obj["entries"]:
        a=row["asset"]
        candles=fetch_fn(a)
        quality=validate_shadow_market(candles,current)
        if quality["completed_utc_day"]!=row["frozen_utc_candle"]:
            raise CandidateLabError("Completed daily market boundary changed")
        grouped[a["symbol"]]=(a,row["spec"])
    created=0
    for asset,spec in grouped.values():
        result=bootstrap(db,[asset],cfg,now=current,fetch_fn=fetch_fn,strategies=(spec,))
        created+=result["created_tracks"]
    _verify_registered(db,obj)
    return {"status":"REGISTERED","created":created,
            "lab_db":str(db),"agent_model_id":obj["model_id"],
            "automatic_timer":False,"real_orders_enabled":False}


def run_lab_cycle(
    plan: str | Path, db: str | Path, *,
    now: datetime | None = None, fetch_fn: Callable = fetch,
    quote_fn: Callable | None = None,
    production_db: str | Path = "state/shadow.db",
) -> dict:
    _guard_isolated(db,production_db)
    obj=read_lab_plan(plan)
    if not Path(db).is_file():
        raise CandidateLabError("First register lab account in separate DB")
    _verify_registered(db,obj)
    assets=[e["asset"] for e in obj["entries"]]
    kwargs={"now":_utc(now),"fetch_fn":fetch_fn}
    if quote_fn is not None:
        kwargs["quote_fn"]=quote_fn
    result=run_cycle(db,assets,**kwargs)
    result["lab_isolated_from_registered_90d"]=True
    result["automatic_timer"]=False
    return result


def main(argv=None) -> int:
    import argparse
    from .settings import load_config
    from .ledger_audit import audit_shadow_ledger
    parser=argparse.ArgumentParser(description="Isolated local LLM factor research lab; no automatic live orders")
    parser.add_argument("action",choices=("propose","register","cycle","status"))
    parser.add_argument("--plan",required=True)
    parser.add_argument("--lab-db",default="state/agent_lab_shadow.db")
    parser.add_argument("--production-db",default="state/shadow.db")
    parser.add_argument("--config",default="examples/shadow_crypto.json")
    parser.add_argument("--model",default="exaone3.5:latest")
    parser.add_argument("--llm-url",default="http://127.0.0.1:11434/v1")
    args=parser.parse_args(argv)
    try:
        cfg,assets=load_config(args.config)
        if args.action=="propose":
            agent=LocalLLMAgent(args.llm_url,args.model,timeout=90)
            result=propose_lab(args.plan,assets=assets,settings=cfg,agent=agent)
        elif args.action=="register":
            result=register_lab(args.plan,args.lab_db,production_db=args.production_db)
        elif args.action=="cycle":
            result=run_lab_cycle(args.plan,args.lab_db,production_db=args.production_db)
        else:
            _guard_isolated(args.lab_db,args.production_db)
            obj=read_lab_plan(args.plan)
            audit=audit_shadow_ledger(args.lab_db)
            if audit["status"]=="PASS":
                _verify_registered(args.lab_db,obj)
            result={"status":audit["status"],"lab_tracks":audit["accounts"],
                    "events":audit["events"],"issues":audit["issues"],
                    "automatic_runner":False,"real_orders_enabled":False}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result.get("status") in ("ERROR","FAIL") else 0
    except (CandidateLabError, OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as e:
        print(f"AGENT LAB FAILED CLOSED: {type(e).__name__}: {e}",file=__import__("sys").stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

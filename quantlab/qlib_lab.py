"""Isolated, manual-only Qlib checkpoint virtual accounts.

Distinct DB: state/qlib_lab_shadow.db; never touches 90-day original paper
experiment or Exaone agent lab. Uses real loaded Qlib LightGBM checkpoint,
not an Agent-authored factor. No automatic timers or broker credentials.
"""
from __future__ import annotations

from datetime import datetime,timezone
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sqlite3

from .prospective import _canonical
from .qlib_frozen import read_qlib_bundle, predict_completed
from .settings import Settings
from .shadow import (
    _account_id, _spec, _utc, _connection, _exclusive_lock,
    _completed_utc_day, _apply, bootstrap,
)
from .quality import validate_shadow_market, validate_public_quote
from .data import fetch, live_paper_quote
from .ledger_audit import audit_shadow_ledger


class QlibLabError(ValueError):
    pass


def _ensure_model_evidence(db: str | Path):
    # Dedicated Qlib lab schema only; never touches other frozen account DBs.
    with _connection(db) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qlib_model_decisions (
              symbol TEXT NOT NULL, decision_date TEXT NOT NULL,
              model_sha256 TEXT NOT NULL, bundle_sha256 TEXT NOT NULL,
              completed_utc_date TEXT NOT NULL,
              predicted_probability REAL NOT NULL,
              threshold REAL NOT NULL, target INTEGER NOT NULL,
              observed_at_utc TEXT NOT NULL,
              PRIMARY KEY (symbol, decision_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qlib_inference_inputs (
              symbol TEXT NOT NULL, decision_date TEXT NOT NULL,
              feature_json TEXT NOT NULL, feature_sha256 TEXT NOT NULL,
              PRIMARY KEY (symbol, decision_date)
            )
        """)


def _isolate(db: str | Path, production: str | Path, agent_db: str | Path):
    dest=Path(db).resolve()
    if (dest.name!="qlib_lab_shadow.db" or dest in {
        Path(production).resolve(),Path(agent_db).resolve()
    }):
        raise QlibLabError("Qlib Lab must use dedicated qlib_lab_shadow.db, not another ledger")


def create_plan(
    plan: str | Path, bundles: list[str | Path], *,
    now: datetime | None = None,
) -> dict:
    current=_utc(now)
    if len(bundles)!=2:
        raise QlibLabError("Frozen Qlib lab requires exactly two BTC/ETH checkpoints")
    entries=[]
    settings=None
    for path in bundles:
        model_info,_=read_qlib_bundle(path)
        if datetime.fromisoformat(model_info["registered_at_utc"]).date()!=current.date():
            raise QlibLabError("Only same-day frozen checkpoints can be registered")
        if model_info["training_last_completed_utc_day"]!=(
            current.date()-__import__("datetime").timedelta(days=1)).isoformat():
            raise QlibLabError("Training cutoff not the latest completed UTC day")
        config=Settings(**model_info["settings"])
        if settings is None:
            settings=asdict(config)
        elif settings!=asdict(config):
            raise QlibLabError("Inconsistent model risk/cash assumptions")
        spec=_spec({
            "kind":"qlib_frozen","label":"qlib_lgbmodel_055",
            "model_sha256":model_info["model_sha256"],
            "manifest_sha256":model_info["sha256"],
            "threshold":.55,
        })
        asset=model_info["asset"]
        entries.append({
            "asset":asset,"spec":spec,
            "account_id":_account_id(asset,spec,config),
            "bundle_dir":str(Path(path).resolve()),
            "training_last_completed_utc_day":model_info["training_last_completed_utc_day"],
        })
    if {e["asset"]["symbol"] for e in entries}!={"KRW-BTC","KRW-ETH"}:
        raise QlibLabError("Expected one frozen BTC and one ETH bundle")
    obj={
        "schema_version":1,"kind":"isolated_qlib_frozen_lightgbm_paper_lab",
        "registered_at_utc":current.isoformat(),"entries":entries,
        "settings":settings,"ledger_basename":"qlib_lab_shadow.db",
        "manual_only":True,"automatic_timer":False,"retraining_allowed":False,
        "real_orders_enabled":False,"automatic_promotion":False,
        "source_training":"microsoft_qlib_LGBModel_actual",
        "historical_test_outcomes_used_for_selection":False,
    }
    obj["sha256"]=sha256(_canonical(obj)).hexdigest()
    output=Path(plan)
    output.parent.mkdir(parents=True,exist_ok=True)
    try:
        with output.open("xb") as fp:
            os.fchmod(fp.fileno(),0o600)
            fp.write(_canonical(obj)+b"\n")
            fp.flush();os.fsync(fp.fileno())
    except FileExistsError as exc:
        raise QlibLabError("Frozen Qlib lab plan already exists") from exc
    return obj


def verify_plan(plan: str | Path, *, check_bundles: bool = True) -> dict:
    obj=json.loads(Path(plan).read_text())
    h=obj.get("sha256")
    if sha256(_canonical({k:v for k,v in obj.items() if k!="sha256"})).hexdigest()!=h:
        raise QlibLabError("Qlib Lab plan hash changed")
    if (obj.get("schema_version")!=1 or obj.get("kind")!="isolated_qlib_frozen_lightgbm_paper_lab"
        or obj.get("ledger_basename")!="qlib_lab_shadow.db"
        or obj.get("manual_only") is not True
        or obj.get("automatic_timer") is not False
        or obj.get("retraining_allowed") is not False
        or obj.get("real_orders_enabled") is not False
        or obj.get("automatic_promotion") is not False):
        raise QlibLabError("Unsupported lab format or unsafe flags")
    entries=obj.get("entries")
    if (not isinstance(entries,list) or len(entries)!=2 or
        {e["asset"]["symbol"] for e in entries}!={"KRW-BTC","KRW-ETH"}):
        raise QlibLabError("Unexpected model universe")
    cfg=Settings(**obj["settings"])
    for e in entries:
        spec=_spec(e["spec"])
        if spec["kind"]!="qlib_frozen" or _account_id(e["asset"],spec,cfg)!=e["account_id"]:
            raise QlibLabError("Frozen account hash invalid")
        if check_bundles:
            model_info,_=read_qlib_bundle(e["bundle_dir"])
            if (model_info["sha256"]!=spec["manifest_sha256"] or
                model_info["model_sha256"]!=spec["model_sha256"] or
                model_info["asset"]!=e["asset"] or
                model_info["training_last_completed_utc_day"]!=e["training_last_completed_utc_day"]):
                raise QlibLabError("Checkpoint changed from frozen lab registration")
    return obj


def _verify_accounts(db: str | Path, obj: dict):
    from .monitor import _read_db
    with _read_db(db) as c:
        accounts=c.execute("SELECT * FROM shadow_accounts").fetchall()
    ids={e["account_id"]:e for e in obj["entries"]}
    if len(accounts)!=2 or {a["track_id"] for a in accounts}!=set(ids):
        raise QlibLabError("Isolated Qlib paper universe changed")
    for a in accounts:
        e=ids[a["track_id"]]
        if (a["symbol"]!=e["asset"]["symbol"] or json.loads(a["spec_json"])!=e["spec"]
            or json.loads(a["settings_json"])!=obj["settings"]
            or a["frozen_bar"]!=e["training_last_completed_utc_day"]):
            raise QlibLabError("Qlib virtual account identity or settings changed")


def register_lab(
    plan: str | Path, db: str | Path, *,
    now: datetime | None = None, fetch_fn=fetch,
    production_db: str | Path = "state/shadow.db",
    agent_db: str | Path = "state/agent_lab_shadow.db",
) -> dict:
    _isolate(db,production_db,agent_db)
    current=_utc(now)
    obj=verify_plan(plan)
    locked=datetime.fromisoformat(obj["registered_at_utc"])
    if locked.date()!=current.date() or current<locked:
        raise QlibLabError("Must register on the original lock UTC date")
    if Path(db).exists():
        raise QlibLabError("Never overwrite or re-register existing Qlib lab")
    cfg=Settings(**obj["settings"])
    for e in obj["entries"]:
        frame=fetch_fn(e["asset"])
        quality=validate_shadow_market(frame,current)
        if quality["completed_utc_day"]!=e["training_last_completed_utc_day"]:
            raise QlibLabError("Training/registration market boundary changed")
    created=0
    for e in obj["entries"]:
        result=bootstrap(db,[e["asset"]],cfg,now=current,
                         fetch_fn=fetch_fn,strategies=(e["spec"],))
        created+=result["created_tracks"]
    _verify_accounts(db,obj)
    _ensure_model_evidence(db)
    return {"status":"REGISTERED","accounts":created,
            "real_orders_enabled":False,"automatic_timer":False}


def run_manual(
    plan: str | Path, db: str | Path, *,
    now: datetime | None = None, fetch_fn=fetch, quote_fn=live_paper_quote,
    production_db: str | Path = "state/shadow.db",
    agent_db: str | Path = "state/agent_lab_shadow.db",
) -> dict:
    _isolate(db,production_db,agent_db)
    current=_utc(now)
    obj=verify_plan(plan)
    if not Path(db).is_file():
        raise QlibLabError("Qlib lab must be registered first")
    _verify_accounts(db,obj)
    _ensure_model_evidence(db)
    audit=audit_shadow_ledger(db)
    if audit["status"]!="PASS":
        raise QlibLabError("Frozen virtual account ledger audit failed")
    from .qlib_decision_audit import audit_decisions
    evidence=audit_decisions(db,obj,model_loader=read_qlib_bundle)
    if evidence["status"]=="FAIL":
        raise QlibLabError("Recorded model probability or event evidence mismatch")
    results=[]
    errors=[]
    # Lock independently; no other timer or paper manager can use this db.
    with _exclusive_lock(db),_connection(db) as conn:
        for item in obj["entries"]:
            sym=item["asset"]["symbol"]
            row=conn.execute("SELECT * FROM shadow_accounts WHERE track_id=?",
                             (item["account_id"],)).fetchone()
            try:
                frame=fetch_fn(item["asset"])
                day=_completed_utc_day(frame,current)
                quality=validate_shadow_market(frame,current)
                if day<=row["frozen_bar"] or conn.execute(
                    "SELECT 1 FROM shadow_events WHERE track_id=? AND decision_date=?",
                    (row["track_id"],day)
                ).fetchone() or row["halted"]:
                    results.append({"symbol":sym,"status":"WAIT_NEW_BAR_OR_ALREADY_RECORDED"})
                    continue
                pred=predict_completed(item["bundle_dir"],frame,now=current)
                if (pred["status"]!="READY" or
                    pred["bundle_manifest_sha256"]!=item["spec"]["manifest_sha256"] or
                    pred["model_sha256"]!=item["spec"]["model_sha256"] or
                    pred["decision_utc_date"]!=day):
                    raise QlibLabError("Frozen Qlib model signal could not be verified")
                price,session=quote_fn(item["asset"]["provider"],sym,now=current)
                if session!=current.date().isoformat():
                    raise QlibLabError("Quote not from current UTC session")
                validate_public_quote(quality["last_completed_close"],price)
                feature_values=pred.get("feature_values")
                feature_digest=pred.get("feature_sha256")
                if (not isinstance(feature_values,list)
                    or len(feature_values)!=9
                    or any(not isinstance(x,(int,float)) or not math.isfinite(x)
                           for x in feature_values)
                    or sha256(_canonical(feature_values)).hexdigest()!=feature_digest
                    or not math.isfinite(pred["probability"])
                    or pred["threshold"]!=item["spec"]["threshold"]
                    or pred["target"]!=int(pred["probability"]>=pred["threshold"])
                    or pred["real_orders_enabled"] is not False):
                    raise QlibLabError("Frozen model inference evidence inconsistent")
                # One atomic transaction for account+event, predicted signal,
                # exact input features and public ticker evidence. Any failure
                # rolls back ALL four; an orphan quote cannot block next cycle.
                with conn:
                    if conn.execute(
                        "SELECT 1 FROM qlib_model_decisions WHERE symbol=? AND decision_date=?",
                        (sym,day)
                    ).fetchone() or conn.execute(
                        "SELECT 1 FROM shadow_quote_evidence WHERE symbol=? AND decision_date=?",
                        (sym,day)
                    ).fetchone() or conn.execute(
                        "SELECT 1 FROM qlib_inference_inputs WHERE symbol=? AND decision_date=?",
                        (sym,day)
                    ).fetchone():
                        raise QlibLabError("Frozen decision/evidence already present: manual review required")
                    event=_apply(conn,row,decision_date=day,session_date=session,
                                 price=float(price),target=int(pred["target"]),now=current)
                    if event["status"]!="RECORDED":
                        raise QlibLabError("Expected a recorded forward-only virtual event")
                    conn.execute(
                        "INSERT INTO qlib_model_decisions "
                        "(symbol,decision_date,model_sha256,bundle_sha256,"
                        "completed_utc_date,predicted_probability,threshold,target,"
                        "observed_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                        (sym,day,pred["model_sha256"],pred["bundle_manifest_sha256"],
                         pred["decision_utc_date"],float(pred["probability"]),
                         float(pred["threshold"]),int(pred["target"]),current.isoformat()),
                    )
                    conn.execute(
                        "INSERT INTO qlib_inference_inputs "
                        "(symbol,decision_date,feature_json,feature_sha256) VALUES(?,?,?,?)",
                        (sym,day,_canonical(feature_values).decode(),feature_digest),
                    )
                    conn.execute(
                        "INSERT INTO shadow_quote_evidence "
                        "(symbol,decision_date,session_date,prior_completed_close,"
                        "public_ticker_price,observed_at_utc,source) VALUES(?,?,?,?,?,?,?)",
                        (sym,day,session,float(quality["last_completed_close"]),
                         float(price),current.isoformat(),"upbit_public_ticker"),
                    )
                results.append({
                    **event,"qlib_probability":pred["probability"],
                    "bundle_sha256":pred["bundle_manifest_sha256"],
                })
            except Exception as exc:
                errors.append({"symbol":sym,"type":type(exc).__name__,
                               "reason":str(exc)[:190]})
        response={
            "status":"ERROR" if errors else "OK",
            "started_at_utc":current.isoformat(),
            "results":results,"errors":errors,
            "automatic_runner":False,"real_orders_enabled":False,
            "isolation":"standalone_qlib_lab",
        }
        with conn:
            conn.execute(
                "INSERT INTO shadow_runs(created_at,status,details_json) VALUES(?,?,?)",
                (current.isoformat(),response["status"],
                 json.dumps(response,sort_keys=True)),
            )
    return response


def main(argv=None):
    import argparse
    from .settings import load_config
    parser=argparse.ArgumentParser(description="Manual, isolated frozen Qlib forward simulation")
    parser.add_argument("action",choices=("plan","register","cycle","status"))
    parser.add_argument("--plan",default="protocols/2026-10-20-qlib-lab.json")
    parser.add_argument("--db",default="state/qlib_lab_shadow.db")
    parser.add_argument("--btc-bundle",default="state/qlib_models/KRW-BTC")
    parser.add_argument("--eth-bundle",default="state/qlib_models/KRW-ETH")
    args=parser.parse_args(argv)
    try:
        if args.action=="plan":
            result=create_plan(args.plan,[args.btc_bundle,args.eth_bundle])
        elif args.action=="register":
            result=register_lab(args.plan,args.db)
        elif args.action=="cycle":
            result=run_manual(args.plan,args.db)
        else:
            _isolate(args.db,"state/shadow.db","state/agent_lab_shadow.db")
            obj=verify_plan(args.plan)
            _verify_accounts(args.db,obj)
            audit=audit_shadow_ledger(args.db)
            from .qlib_decision_audit import audit_decisions
            evidence=audit_decisions(args.db,obj,model_loader=read_qlib_bundle)
            result={"status":"FAIL" if evidence["status"]=="FAIL" else audit["status"],
                    "decision_replay":evidence,"accounts":audit["accounts"],
                    "events":audit["events"],"manual_only":True,
                    "real_orders_enabled":False}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result.get("status") in ("FAIL","ERROR") else 0
    except (QlibLabError,RuntimeError,OSError,ValueError,sqlite3.Error,KeyError) as exc:
        import sys
        print(f"QLIB LAB FAILED CLOSED: {type(exc).__name__}: {exc}",file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

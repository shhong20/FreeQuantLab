"""Forensic verification of recorded Qlib inference to virtual execution.

Historical Qlib prediction inputs saved at the instant of forward simulated
decision; exact model/golden bundle replay validates each probability, target,
quote and event. Not a proof of real fills, alpha, or unobserved market prices.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3

import numpy as np

from .monitor import _read_db
from .prospective import _canonical
from .qlib_frozen import read_qlib_bundle


def audit_decisions(db: str | Path, plan: dict, *, model_loader=read_qlib_bundle) -> dict:
    issues=[]
    with _read_db(db) as c:
        c.execute("BEGIN")
        events=c.execute(
            "SELECT e.*,a.symbol FROM shadow_events e "
            "JOIN shadow_accounts a ON a.track_id=e.track_id"
        ).fetchall()
        decisions=c.execute("SELECT * FROM qlib_model_decisions").fetchall()
        inputs=c.execute("SELECT * FROM qlib_inference_inputs").fetchall() if (
            c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                      "AND name='qlib_inference_inputs'").fetchone()
        ) else []
        quotes=c.execute("SELECT * FROM shadow_quote_evidence").fetchall()
        c.execute("COMMIT")
    def key(row):
        return (row["symbol"],row["decision_date"])
    maps=[]
    for rows in (events,decisions,inputs,quotes):
        mapping={}
        for row in rows:
            k=key(row)
            if k in mapping:
                issues.append({"code":"DUPLICATE_DECISION_KEY","symbol":k[0],"day":k[1]})
            mapping[k]=row
        maps.append(mapping)
    event_map,decision_map,input_map,quote_map=maps
    all_keys=set().union(*(set(m) for m in maps))
    expected={item["asset"]["symbol"]:item for item in plan["entries"]}
    boosters={}
    for symbol,item in expected.items():
        body,booster=model_loader(item["bundle_dir"])
        if (body["sha256"]!=item["spec"]["manifest_sha256"] or
            body["model_sha256"]!=item["spec"]["model_sha256"]):
            raise ValueError("Qlib model changed since frozen plan")
        boosters[symbol]=booster
    matched=0
    for k in sorted(all_keys):
        symbol,day=k
        if symbol not in expected:
            issues.append({"code":"OUTSIDE_FROZEN_QLIB_UNIVERSE","symbol":symbol,"day":day})
            continue
        if not all(k in m for m in maps):
            issues.append({"code":"MISSING_ATOMIC_QLIB_EVIDENCE","symbol":symbol,"day":day})
            continue
        e,d,x,q=(m[k] for m in maps)
        item=expected[symbol]
        codes=[]
        if (e["track_id"]!=item["account_id"] or
            d["completed_utc_date"]!=day or e["session_date"]!=q["session_date"]):
            codes.append("DECISION_ACCOUNT_SESSION_MISMATCH")
        if (d["bundle_sha256"]!=item["spec"]["manifest_sha256"] or
            d["model_sha256"]!=item["spec"]["model_sha256"] or
            d["threshold"]!=item["spec"]["threshold"]):
            codes.append("MODEL_IDENTITY_OR_THRESHOLD_MISMATCH")
        try:
            vector=json.loads(x["feature_json"])
            valid=(isinstance(vector,list) and len(vector)==9 and
                   all(type(v) in (float,int) and math.isfinite(v) for v in vector))
            if not valid or sha256(_canonical(vector)).hexdigest()!=x["feature_sha256"]:
                codes.append("FEATURE_VECTOR_HASH_MISMATCH")
            else:
                prediction=float(np.asarray(
                    boosters[symbol].predict(np.asarray(vector,dtype=float).reshape(1,-1))
                ).reshape(-1)[0])
                if (not math.isfinite(prediction) or
                    not math.isclose(prediction,float(d["predicted_probability"]),
                                     rel_tol=1e-11,abs_tol=1e-11)):
                    codes.append("REPLAYED_MODEL_PROBABILITY_MISMATCH")
        except (ValueError,TypeError,IndexError,KeyError):
            codes.append("MODEL_REPLAY_FAILED")
        target=int(d["predicted_probability"]>=d["threshold"]) if (
            math.isfinite(float(d["predicted_probability"])) and
            math.isfinite(float(d["threshold"]))
        ) else -1
        if target!=d["target"] or d["target"]!=e["target"]:
            codes.append("MODEL_SIGNAL_TO_VIRTUAL_EVENT_MISMATCH")
        if (e["created_at"]!=d["observed_at_utc"] or
            e["created_at"]!=q["observed_at_utc"]):
            codes.append("MODEL_QUOTE_EVENT_TIME_MISMATCH")
        if not math.isclose(float(e["observed_price"]),
                            float(q["public_ticker_price"]),rel_tol=1e-10,abs_tol=1e-7):
            codes.append("MODEL_QUOTE_EVENT_PRICE_MISMATCH")
        for code in dict.fromkeys(codes):
            issues.append({"code":code,"symbol":symbol,"day":day})
        if not codes:
            matched+=1
    return {
        "status":"FAIL" if issues else "AWAITING_FIRST_DECISION" if not all_keys else "PASS",
        "qlib_model_events":len(event_map),
        "fully_replayed_decisions":matched,
        "missing_or_mismatched_decisions":len(all_keys)-matched,
        "model_count":len(boosters),
        "issues":issues[:100],
        "actual_trade_execution_verified":False,
        "broker_orders_enabled":False,
        "explanatory_note":"Exact saved feature/model probability-to-virtual-event consistency only.",
    }

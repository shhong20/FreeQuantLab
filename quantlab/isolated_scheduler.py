"""Explicitly authorized, bounded, frozen-paper-only automation for isolated labs.

An independent Git-versioned operational authorization supplements (never edits)
the originally manual-only strategy manifests. It permits daily virtual
observations only, not strategy research, retraining or live broker orders.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

from .prospective import _canonical, _utc
from .ledger_audit import audit_shadow_ledger
from .quote_quality import execution_proxy_quality
from .candidate_lab import read_lab_plan, _verify_registered, run_lab_cycle
from .qlib_lab import verify_plan, _verify_accounts, run_manual
from .qlib_decision_audit import audit_decisions

DEFAULT_POLICY = "ops/isolated_paper_scheduler.json"
VALID_LANES = ("exaone", "qlib")
REQUIRED_DB_NAMES = {"exaone": "agent_lab_shadow.db", "qlib": "qlib_lab_shadow.db"}


class SchedulerError(ValueError):
    pass


def read_schedule(path: str | Path = DEFAULT_POLICY) -> dict:
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise SchedulerError("Invalid operational authorization format")
    token = policy.get("sha256")
    if not isinstance(token, str) or sha256(
        _canonical({k: v for k, v in policy.items() if k != "sha256"})
    ).hexdigest() != token:
        raise SchedulerError("Operational authorization hash invalid")
    if (policy.get("mode") != "virtual_observation_only"
        or policy.get("allow_live_broker_orders") is not False
        or policy.get("allow_model_retraining") is not False
        or policy.get("allow_auto_promotion") is not False
        or policy.get("allow_historical_backfill") is not False
        or policy.get("requires_original_frozen_strategies") is not True
        or policy.get("activation_basis") != "user_authorized_isolated_automation_2026-10-20"):
        raise SchedulerError("Operational policy requests unsupported permissions")
    if set(policy.get("lanes", {})) != set(VALID_LANES):
        raise SchedulerError("Only the two isolated research lanes are supported")
    first = date.fromisoformat(policy["first_decision_utc"])
    last = date.fromisoformat(policy["last_decision_utc"])
    if first != date(2026, 10, 20) or last != date(2027, 1, 18):
        raise SchedulerError("Unexpected decision dates")
    if datetime.fromisoformat(policy["registered_at_utc"]).tzinfo is None:
        raise SchedulerError("No timezone in policy registration")
    for name in VALID_LANES:
        item = policy["lanes"][name]
        if (Path(item["db"]).name != REQUIRED_DB_NAMES[name]
            or len(item["plan_sha256"]) != 64
            or item["expected_frozen_tracks"] != 2
            or item.get("auto_paper_only") is not True):
            raise SchedulerError("Invalid isolated lane policy")
    for source in ("production_db", "exaone_db", "qlib_db"):
        if source not in policy:
            raise SchedulerError("Missing ledger binding")
    paths = [Path(policy[x]).resolve() for x in (
        "production_db", "exaone_db", "qlib_db")]
    if (len(set(paths)) != 3 or [x.name for x in paths] !=
        ["shadow.db", "agent_lab_shadow.db", "qlib_lab_shadow.db"]):
        raise SchedulerError("Ledger isolation is not preserved")
    for name, key in (("exaone", "exaone_db"), ("qlib", "qlib_db")):
        if Path(policy["lanes"][name]["db"]).resolve() != Path(policy[key]).resolve():
            raise SchedulerError("Lane database does not match frozen authorization")
    return policy


def verify_lane(policy: dict, name: str, *, now: datetime | None = None):
    if name not in VALID_LANES:
        raise SchedulerError("Unsupported research lane")
    instant = _utc(now)
    registration = datetime.fromisoformat(policy["registered_at_utc"])
    if instant < registration:
        raise SchedulerError("Clock precedes automation authorization")
    item = policy["lanes"][name]
    plan = (read_lab_plan(item["plan"]) if name == "exaone"
            else verify_plan(item["plan"]))
    if plan["sha256"] != item["plan_sha256"]:
        raise SchedulerError("Strategy plan changed since automation authorization")
    if name == "exaone":
        _verify_registered(item["db"], plan)
        if plan.get("broker_orders_enabled") is not False:
            raise SchedulerError("Untrusted Exaone strategy permissions")
    else:
        _verify_accounts(item["db"], plan)
        if plan.get("real_orders_enabled") is not False:
            raise SchedulerError("Untrusted Qlib strategy permissions")
    ledger = audit_shadow_ledger(item["db"], now=instant)
    if ledger["status"] != "PASS" or ledger["accounts"] != 2:
        raise SchedulerError("Existing virtual ledger failed frozen accounting audit")
    quote = execution_proxy_quality(item["db"])
    if quote["status"] not in ("PASS", "AWAITING_FIRST_OBSERVATION") or quote["legacy_events_without_quote_evidence"]:
        raise SchedulerError("Saved public quote evidence is missing or inconsistent")
    if name == "qlib":
        replay = audit_decisions(item["db"], plan)
        if replay["status"] == "FAIL":
            raise SchedulerError("Qlib model decisions do not replay exactly")
    return plan


def eligible_decision(policy: dict, now: datetime) -> date | None:
    """Only yesterday's fully closed UTC candle; never fill older missing days."""
    first = date.fromisoformat(policy["first_decision_utc"])
    last = date.fromisoformat(policy["last_decision_utc"])
    day = now.date() - timedelta(days=1)
    return day if first <= day <= last else None


def run_scheduled(
    lane: str, *, policy_path: str | Path = DEFAULT_POLICY,
    now: datetime | None = None,
    agent_runner=None, qlib_runner=None,
) -> dict:
    instant = _utc(now)
    policy = read_schedule(policy_path)
    plan = verify_lane(policy, lane, now=instant)
    day = eligible_decision(policy, instant)
    if day is None:
        return {"status": "OUTSIDE_AUTHORIZED_UTC_WINDOW",
                "lane": lane, "real_orders_enabled": False, "virtual_events": 0}
    # No in-session model selection or change to existing risk or leverage.
    item = policy["lanes"][lane]
    if lane == "exaone":
        runner = agent_runner or run_lab_cycle
        result = runner(item["plan"], item["db"], now=instant,
                        production_db=policy["production_db"])
    else:
        runner = qlib_runner or run_manual
        result = runner(item["plan"], item["db"], now=instant,
                        production_db=policy["production_db"],
                        agent_db=policy["agent_db"])
    if result.get("status") != "OK" or result.get("real_orders_enabled") is not False:
        raise SchedulerError("Isolated virtual research observation failed closed")
    return {"status": "OK", "lane": lane, "decision_utc_day": day.isoformat(),
            "lane_plan_sha256": plan["sha256"],
            "results": result.get("results", []),
            "real_orders_enabled": False, "model_retrained": False,
            "automatic_promotion": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Authorized frozen-model research paper observations only")
    parser.add_argument("lane", choices=VALID_LANES)
    parser.add_argument("--policy", default=DEFAULT_POLICY)
    args = parser.parse_args(argv)
    try:
        outcome = run_scheduled(args.lane, policy_path=args.policy)
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print("ISOLATED PAPER CYCLE BLOCKED: "
              + type(exc).__name__ + ": " + str(exc)[:220], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

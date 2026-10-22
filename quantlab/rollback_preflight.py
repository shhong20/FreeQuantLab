"""Immutable-baseline rollback dry-run. Never deploys or mutates runtime.

A previous deployment SHA is not sufficient authorization to switch running
services: schema, unit commands, models and prospective protocols must match.
All discovered changes require independent review. A frozen clean cohort is
explicitly protected against mid-study strategy or protocol changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Callable

from .deploy_contract import (
    SCHEMA_VERSION, _fingerprint, check_deployment, inspect_deployment,
    SERVICES_ALL, EVIDENCE_FILES,
)

RUNNER = subprocess.run
PROTECTED_CLEAN_PREFIXES = (
    "quantlab/shadow.py",
    "quantlab/clean_cohort.py",
    "quantlab/clean_first_day_attestation.py",
    "quantlab/clean_backup.py",
    "quantlab/ledger_audit.py",
    "quantlab/monitor.py",
    "quantlab/prospective.py",
    "quantlab/protocol_compliance.py",
    "quantlab/settings.py",
    "quantlab/features.py",
    "examples/clean_shadow_crypto.json",
    "protocols/2026-10-21-btc-eth-clean-90d.json",
)
# Only known read-only observability modules are exempt from a frozen study
# contract change. New/unclassified application code must be reviewed as a
# potentially critical change rather than silently treated as harmless.
READONLY_OBSERVABILITY_MODULES = frozenset({
    "quantlab/deploy_contract.py",
    "quantlab/ops_integrity.py",
    "quantlab/rollback_preflight.py",
    "quantlab/halt_exposure.py",
})
PROTECTED_UNIT_PREFIX = "deploy/systemd/freequant-clean-"


class RollbackBlocked(ValueError):
    pass


def _read_manifest(path: str | Path) -> dict:
    # Never accept the caller-provided digest without computing its content.
    manifest = json.loads(Path(path).read_text("utf-8"))
    if (manifest.get("schema") != SCHEMA_VERSION or
        manifest.get("identity", {}).get("schema") != SCHEMA_VERSION or
        manifest.get("identity_sha256") != _fingerprint(manifest["identity"])):
        raise RollbackBlocked("Invalid or altered deployment baseline")
    return manifest


def _git(repo: Path, argv: list[str], runner: Callable = RUNNER) -> str:
    result = runner(
        ["git", "-C", str(repo), *argv],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode != 0:
        raise RollbackBlocked("Immutable git object or Git metadata unavailable")
    return result.stdout.strip()


def analyze_rollback(
    active: dict, target: dict, lane: str, *,
    runner: Callable = RUNNER,
) -> dict:
    """Compare two signed-by-content deployment manifests and archived Git objects.

    Returns a report only; cannot change the worktree or systemd state.
    """
    for report in (active, target):
        if report.get("schema") != SCHEMA_VERSION or (
            report.get("identity_sha256") != _fingerprint(report["identity"])
        ):
            raise RollbackBlocked("Manifest fingerprint mismatch")
    active_id, target_id = active["identity"], target["identity"]
    if lane not in ("core", "observer", "clean"):
        raise RollbackBlocked("Unknown deployment lane")
    if set(active_id["lanes"]) != set(target_id["lanes"]):
        raise RollbackBlocked("Runtime lane set mismatch")
    current = active_id["lanes"][lane]
    proposed = target_id["lanes"][lane]
    if proposed["path"] != current["path"]:
        raise RollbackBlocked("Different runtime directory cannot be reused as rollback")
    repo = Path(current["path"]).resolve(strict=True)
    if not current["git_worktree_clean"]:
        raise RollbackBlocked("Current runtime is dirty")
    for name in ("git_sha",):
        for item in (current, proposed):
            if not re.fullmatch(r"[a-f0-9]{40}", item[name]):
                raise RollbackBlocked("Not a pinned git SHA")
    # Only trusted Git objects that remain in the original repository are eligible.
    _git(repo, ["cat-file", "-e", proposed["git_sha"] + "^{commit}"], runner)
    head = _git(repo, ["rev-parse", "HEAD"], runner)
    if head != current["git_sha"]:
        raise RollbackBlocked("Live runtime changed since inspection")
    # A rollback means returning to an actual ancestor of the pinned runtime,
    # never jumping sideways to an unrelated commit posing as a prior release.
    _git(repo, ["merge-base", "--is-ancestor", proposed["git_sha"], head], runner)
    changed = sorted(filter(None, _git(
        repo, ["diff", "--name-only", proposed["git_sha"], current["git_sha"]], runner,
    ).splitlines()))
    protected_files = [f for f in changed
                       if f in PROTECTED_CLEAN_PREFIXES or
                       f.startswith(PROTECTED_UNIT_PREFIX) or
                       (f.startswith("quantlab/") and
                        f.endswith(".py") and
                        f not in READONLY_OBSERVABILITY_MODULES)]
    constraints = []
    warnings = []
    if proposed["db_schema"] != current["db_schema"]:
        constraints.append("SCHEMA_CHANGE_REQUIRES_MIGRATION_AND_RESTORE_PLAN")
    if proposed["evidence_sha256"] != current["evidence_sha256"]:
        constraints.append("PREREGISTRATION_OR_POLICY_IDENTITY_CHANGED")
    old_units = {k:v for k,v in target_id["units"].items() if v["lane"] == lane}
    new_units = {k:v for k,v in active_id["units"].items() if v["lane"] == lane}
    if set(old_units) != set(new_units):
        constraints.append("UNIT_SET_CHANGED")
    service_changes = sorted(k for k in old_units
                             if k in new_units and old_units[k] != new_units[k])
    if service_changes:
        constraints.append("EFFECTIVE_SERVICE_OR_TEMPLATE_CHANGED")
    for other in sorted(set(active_id["lanes"]) - {lane}):
        if target_id["lanes"][other]["git_sha"] != active_id["lanes"][other]["git_sha"]:
            warnings.append("BASELINE_OLDER_OTHER_LANE:" + other)
    if lane == "clean" and protected_files:
        constraints.append("ACTIVE_CLEAN_COHORT_CONTRACT_CODE_CHANGED")
    if target_id.get("frozen_qlib_artifacts") != active_id.get("frozen_qlib_artifacts"):
        warnings.append("QLIB_MODEL_MANIFEST_COVERAGE_DIFFERS_OR_CHANGED")
    if not proposed["git_worktree_clean"]:
        constraints.append("ARCHIVED_BASELINE_RECORDED_DIRTY_WORKTREE")
    if proposed["git_sha"] == current["git_sha"]:
        status = "NO_ROLLBACK_NEEDED" if not constraints else "ROLLBACK_BLOCKED"
    elif constraints:
        status = "ROLLBACK_BLOCKED"
    else:
        status = "MANUAL_REVIEW_REQUIRED"
    return {
        "status": status,
        "lane": lane,
        "current_sha": current["git_sha"],
        "target_sha": proposed["git_sha"],
        "different_tracked_paths": changed,
        "protected_clean_paths_changed": protected_files if lane == "clean" else [],
        "unit_changes": service_changes,
        "blockers": sorted(set(constraints)),
        "cautions": sorted(set(warnings)),
        "candidate_git_commit_found": True,
        "deployment_attempted": False,
        "rollback_attempted": False,
        "service_restarted": False,
        "live_ledger_modified": False,
        "approval_to_switch": False,
        "real_orders_enabled": False,
        "automatic_model_promotion": False,
    }


def main(argv:list[str]|None=None) -> int:
    parser = argparse.ArgumentParser(description="Immutable deployment rollback DRY-RUN only")
    parser.add_argument("--current-baseline", required=True)
    parser.add_argument("--target-baseline", required=True)
    parser.add_argument("--lane", choices=("core","observer","clean"), required=True)
    options = parser.parse_args(argv)
    try:
        current = inspect_deployment()
        approved = _read_manifest(options.current_baseline)
        deployed = check_deployment(approved, current)
        if deployed["drift"]:
            raise RollbackBlocked("Actual active deployment does not match approved baseline")
        target = _read_manifest(options.target_baseline)
        result = analyze_rollback(approved, target, options.lane)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0 if result["status"]=="NO_ROLLBACK_NEEDED" else 2
    except (RollbackBlocked, OSError, RuntimeError, ValueError, KeyError, TypeError,
            sqlite3.Error, json.JSONDecodeError) as error:
        print(json.dumps({
            "status": "ROLLBACK_DRYRUN_FAIL_CLOSED",
            "reason": type(error).__name__, "service_restarted": False,
            "automatic_rollback": False, "real_orders_enabled": False,
        }), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

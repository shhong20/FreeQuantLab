"""Read-only verifiable deployment snapshot for three isolated FreeQuantLab runtimes.

Capture records source revisions, effective systemd units, frozen protocol
digests and state-schema metadata. Verify compares immutable deployment
identities, NOT dynamic unit exit codes. No git switching, service restarts,
SQLite writes, model promotion or orders. Operational failures are reported
separately from deployment drift.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Callable

from .ops_integrity import RUNTIMES, SERVICES, _run
from .prospective import read_protocol, _canonical

SCHEMA_VERSION = 1
SERVICES_ALL = {**SERVICES, "freequant-clean-backup.service": "clean",
                "freequant-market-gap-monitor.service": "clean"}
EVIDENCE_FILES = {
    "core": (
        "ops/isolated_paper_scheduler.json",
        "protocols/2026-10-20-exaone-agent-lab.json",
        "protocols/2026-10-20-qlib-lab.json",
    ),
    "clean": (
        "protocols/2026-10-21-btc-eth-clean-90d.json",
        "examples/clean_shadow_crypto.json",
    ),
    "observer": (),
}
STATE_DB_FILES = {
    "core": ("state/shadow.db", "state/agent_lab_shadow.db",
             "state/qlib_lab_shadow.db"),
    "clean": ("state/clean_shadow_90d.db",),
}
SHARED_STATE = RUNTIMES["core"] / "state"


def _sha(value: bytes) -> str:
    return sha256(value).hexdigest()


def _fingerprint(obj: dict) -> str:
    return _sha(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8"))


def _source(unit: str) -> str:
    # Omit ephemeral systemd timestamps/PIDs/status fields from identity.
    # argv[]= is the stable effective command line, including all ExecStart
    # stages of Type=oneshot services.
    matches = re.findall(r"argv\[\]=(.+?) ; ignore_errors=", unit)
    if not matches:
        raise ValueError("systemd ExecStart has no canonical argv")
    return "\n".join(matches)


def _show(unit: str, runner: Callable) -> dict:
    output = _run(["systemctl", "--user", "show", unit,
                   "-p", "WorkingDirectory", "-p", "FragmentPath",
                   "-p", "ExecStart", "-p", "Result",
                   "-p", "ExecMainStatus"], runner)
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if line.startswith("ExecStart="):
            fields["ExecStart"] = fields.get("ExecStart", "") + line + "\n"
        elif "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return fields


def qlib_artifact_inventory(*, primary: Path | None = None,
                            mirror: Path | None = None,
                            features: Path | None = None) -> dict:
    """SHA-check actual BTC/ETH Qlib model artifacts and frozen mirror."""
    primary = SHARED_STATE / "qlib_models" if primary is None else primary
    mirror = RUNTIMES["core"] / "models/qlib" if mirror is None else mirror
    features = RUNTIMES["core"] / "quantlab/features.py" if features is None else features
    feature_sha = _sha(features.read_bytes())
    models = {}
    for market in ("KRW-BTC", "KRW-ETH"):
        copies = []
        for root in (primary, mirror):
            folder = (root / market).resolve(strict=True)
            if not folder.is_dir() or not folder.is_relative_to(root.resolve(strict=True)):
                raise ValueError("Model folder escaped pinned artifact directory")
            fingerprints = {}
            for name in ("manifest.json", "golden.json", "model.txt"):
                file = folder / name
                if file.is_symlink() or not file.is_file():
                    raise ValueError("Missing or symlinked frozen Qlib file")
                fingerprints[name] = _sha(file.read_bytes())
            manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
            golden = json.loads((folder / "golden.json").read_text("utf-8"))
            if (manifest.get("model_sha256") != fingerprints["model.txt"] or
                manifest.get("golden_sha256") != _sha(_canonical(golden)) or
                manifest.get("sha256") != _sha(_canonical({
                    k: v for k, v in manifest.items() if k != "sha256"})) or
                manifest.get("features_py_sha256") != feature_sha or
                manifest.get("asset", {}).get("symbol") != market or
                manifest.get("real_orders_enabled") is not False or
                manifest.get("promotion_enabled") is not False or
                manifest.get("retraining_enabled") is not False):
                raise ValueError("Frozen Qlib model manifest identity mismatch")
            copies.append(fingerprints)
        if copies[0] != copies[1]:
            raise ValueError("Qlib original and frozen mirror differ")
        models[market] = copies[0]
    return {"models":models, "features_py_sha256":feature_sha,
            "source_and_mirror_match": True,
            "model_inference_validated_by_this_command": False,
            "exaone_weights_attested": False}


def _sqlite_schema(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        db.execute("PRAGMA query_only=ON")
        tables = sorted(row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "name NOT LIKE 'sqlite_%'"))
        version = db.execute("PRAGMA user_version").fetchone()[0]
        # Schema identity, NOT mutable DB contents or live PnL.
        ddl = sorted(row for row in db.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger','view') AND sql IS NOT NULL"))
    return {"sqlite_user_version": int(version), "schema_sha256":
            _fingerprint({"ddl": ddl}), "table_count": len(tables)}


def inspect_deployment(*, roots: dict[str, Path] | None = None,
                       services: dict[str, str] | None = None,
                       evidence: dict[str, tuple[str, ...]] | None = None,
                       state: dict[str, tuple[str, ...]] | None = None,
                       shared_state: Path | None = None,
                       runner: Callable = subprocess.run,
                       qlib_artifacts: bool = True) -> dict:
    roots = RUNTIMES if roots is None else roots
    services = SERVICES_ALL if services is None else services
    evidence = EVIDENCE_FILES if evidence is None else evidence
    state = STATE_DB_FILES if state is None else state
    shared_state = SHARED_STATE if shared_state is None else shared_state
    revisions: dict[str, dict] = {}
    for lane, root in sorted(roots.items()):
        root = root.resolve(strict=True)
        sha = _run(["git", "-C", str(root), "rev-parse", "HEAD"], runner)
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid Git HEAD identity")
        clean = not _run(["git", "-C", str(root), "status", "--porcelain"], runner)
        files: dict[str, str] = {}
        for relative in evidence.get(lane, ()):
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("Unexpected evidence file path")
            files[relative] = _sha(path.read_bytes())
            if lane == "clean" and relative.startswith("protocols/"):
                plan = read_protocol(path)
                if (len(plan["account_set"]) != 6 or
                    plan["automatic_strategy_promotion"] is not False or
                    plan["real_broker_order_permission"] is not False):
                    raise ValueError("Frozen clean protocol controls changed")
        db_schemas: dict[str, dict] = {}
        for relative in state.get(lane, ()):
            filename = (shared_state / Path(relative).name)
            db_schemas[relative] = _sqlite_schema(filename)
        revisions[lane] = {
            "path": str(root), "git_sha": sha, "git_worktree_clean": clean,
            "evidence_sha256": files, "db_schema": db_schemas,
        }
    units: dict[str, dict] = {}
    for unit, lane in sorted(services.items()):
        if lane not in revisions:
            raise ValueError("Service uses unknown lane")
        raw = _show(unit, runner)
        expected = revisions[lane]["path"]
        fragment = Path(raw.get("FragmentPath", ""))
        if not fragment.is_absolute() or not fragment.is_file():
            raise ValueError("Unit definition file missing")
        installed = fragment.read_bytes()
        source_template = Path(expected) / "deploy/systemd" / unit
        if not source_template.is_file():
            raise ValueError("Expected checked-in unit definition missing")
        source_bytes = source_template.read_bytes()
        units[unit] = {
            "lane": lane,
            "working_directory": raw.get("WorkingDirectory", ""),
            "working_directory_matches_lane": raw.get("WorkingDirectory") == expected,
            "unit_fragment_sha256": _sha(installed),
            "unit_template_sha256": _sha(source_bytes),
            "installed_unit_matches_template": installed == source_bytes,
            "effective_exec_start_sha256": _sha(_source(raw["ExecStart"]).encode("utf-8")),
            # Diagnostic state only: NEVER a release identity.
            "observed_result": raw.get("Result"),
            "observed_exit_code": raw.get("ExecMainStatus"),
        }
    identity = {"schema": SCHEMA_VERSION, "lanes": revisions,
                "units": {k: {j: v for j, v in item.items()
                              if not j.startswith("observed_")}
                          for k, item in units.items()}}
    if qlib_artifacts:
        identity["frozen_qlib_artifacts"] = qlib_artifact_inventory()
    return {
        "schema": SCHEMA_VERSION,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "identity": identity,
        "identity_sha256": _fingerprint(identity),
        "observed_service_results": {
            u: {"result": data["observed_result"],
                "exit_code": data["observed_exit_code"]}
            for u, data in units.items()},
        "safe_to_autodeploy": False,
        "models_full_binary_sha256_verified": False,
        "frozen_qlib_artifact_sha256_verified": qlib_artifacts,
        "source_database_contents_attested": False,
        "real_order_execution_enabled": False,
        "automatic_strategy_promotion_enabled": False,
    }


def check_deployment(report: dict, current: dict) -> dict:
    if report.get("schema") != SCHEMA_VERSION:
        raise ValueError("Unknown baseline schema")
    expected = report["identity"]
    if report.get("identity_sha256") != _fingerprint(expected):
        raise ValueError("Stored deployment manifest fingerprint was modified")
    actual = current["identity"]
    drift = []
    if set(expected["lanes"]) != set(actual["lanes"]):
        drift.append("RUNTIME_LANE_SET_CHANGED")
    for lane in sorted(set(expected["lanes"]) | set(actual["lanes"])):
        if expected["lanes"].get(lane) != actual["lanes"].get(lane):
            drift.append("LANE_IDENTITY_CHANGED:" + lane)
    if expected.get("frozen_qlib_artifacts") != actual.get("frozen_qlib_artifacts"):
        drift.append("FROZEN_QLIB_MODEL_ARTIFACT_CHANGED")
    if set(expected["units"]) != set(actual["units"]):
        drift.append("SERVICE_SET_CHANGED")
    for unit in sorted(set(expected["units"]) | set(actual["units"])):
        if expected["units"].get(unit) != actual["units"].get(unit):
            drift.append("SERVICE_DEFINITION_CHANGED:" + unit)
    # Dirty worktree or installed/template mismatch are unsafe even if both
    # snapshots agree (capturing a bad state must not bless it).
    for lane, data in actual["lanes"].items():
        if not data["git_worktree_clean"]:
            drift.append("DIRTY_RUNTIME:" + lane)
    for unit, data in actual["units"].items():
        if not data["installed_unit_matches_template"]:
            drift.append("UNIT_TEMPLATE_DRIFT:" + unit)
        if not data["working_directory_matches_lane"]:
            drift.append("UNIT_RUNTIME_DRIFT:" + unit)
    return {
        "status": "DEPLOYMENT_IDENTITY_MATCH" if not drift else "DEPLOYMENT_IDENTITY_DRIFT",
        "drift": sorted(set(drift)),
        "known_service_failures_are_separate_from_release_drift": True,
        "automatic_git_switch_or_rollback_performed": False,
        "real_orders_enabled": False,
        "model_auto_promotion": False,
    }


def save_baseline(path: str | Path, report: dict) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(report, indent=2, sort_keys=True,
                       ensure_ascii=False) + "\n").encode("utf-8")
    prior = os.umask(0o077)
    try:
        with dest.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    finally:
        os.umask(prior)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Capture/verify non-mutating 3-runtime deployment identity")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--capture", metavar="OUTPUT_JSON")
    group.add_argument("--verify", metavar="BASELINE_JSON")
    a = p.parse_args(argv)
    try:
        current = inspect_deployment()
        if a.capture:
            baseline = check_deployment(current, current)
            if baseline["status"] != "DEPLOYMENT_IDENTITY_MATCH":
                raise ValueError("Refuse baseline creation from already drifted runtime")
            save_baseline(a.capture, current)
            print(json.dumps({"status": "BASELINE_CAPTURED",
                              "identity_sha256": current["identity_sha256"],
                              "service_count": len(current["identity"]["units"]),
                              "automatic_deploy": False}, sort_keys=True))
            return 0
        expected = json.loads(Path(a.verify).read_text("utf-8"))
        result = check_deployment(expected, current)
        print(json.dumps(result, sort_keys=True))
        return 0 if not result["drift"] else 2
    except (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error,
            TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "DEPLOYMENT_VERIFICATION_FAILED",
                          "reason": type(exc).__name__,
                          "automatic_deploy": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

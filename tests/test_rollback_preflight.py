"""Rollback planning cannot write a ledger, switch Git SHA, or restart units."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantlab.deploy_contract import _fingerprint
from quantlab.rollback_preflight import (
    RollbackBlocked, analyze_rollback, _read_manifest, main,
)


def _snap(root, *, sha="a"*40, schema="schema-1", policy="policy-1", units="unit-1"):
    lanes = {name: {
        "path": str(root),
        "git_sha": sha if name == "clean" else ("c" if name == "core" else "d")*40,
        "git_worktree_clean": True,
        "evidence_sha256": {"protocol": policy} if name=="clean" else {},
        "db_schema": {"db":schema} if name=="clean" else {},
    } for name in ("core", "observer", "clean")}
    identity = {
        "schema":1,
        "lanes":lanes,
        "units":{
            "clean.service":{
                "lane":"clean",
                "unit_template_sha256":units,
                "installed_unit_matches_template":True,
                "working_directory_matches_lane":True,
            },
        },
        "frozen_qlib_artifacts":{"KRW-BTC":"frozen"},
    }
    return {"schema":1, "identity":identity,
            "identity_sha256":_fingerprint(identity)}


def _runner(current_sha, changed=()):
    called = []
    def fake(args, **kwargs):
        called.append(args)
        assert args[:3] == ["git","-C",args[2]]
        cmd=args[3:]
        assert cmd[0] in ("cat-file","rev-parse","merge-base","diff")
        if cmd[0]=="cat-file":
            result=""
        elif cmd[0]=="rev-parse":
            result=current_sha+"\n"
        else:
            result="\n".join(changed)
        return SimpleNamespace(returncode=0, stdout=result, stderr="")
    return fake, called


def test_doc_only_clean_rollback_cannot_execute_without_review(tmp_path):
    older=_snap(tmp_path,sha="b"*40)
    current=_snap(tmp_path)
    run, calls =_runner("a"*40,["docs/README.md"])
    result=analyze_rollback(current,older,"clean",runner=run)
    assert result["status"]=="MANUAL_REVIEW_REQUIRED"
    assert result["blockers"]==[]
    assert not result["deployment_attempted"]
    assert not result["approval_to_switch"]
    assert [argv[3] for argv in calls]==["cat-file","rev-parse","merge-base","diff"]


def test_frozen_strategy_changes_block_mid_study_rollback(tmp_path):
    older=_snap(tmp_path,sha="b"*40)
    current=_snap(tmp_path)
    run,_=_runner("a"*40,["quantlab/shadow.py","tests/test_shadow.py"])
    result=analyze_rollback(current,older,"clean",runner=run)
    assert result["status"]=="ROLLBACK_BLOCKED"
    assert "ACTIVE_CLEAN_COHORT_CONTRACT_CODE_CHANGED" in result["blockers"]
    assert result["protected_clean_paths_changed"]==["quantlab/shadow.py"]


def test_protocol_and_schema_mismatch_block(tmp_path):
    older=_snap(tmp_path,sha="b"*40,schema="older",policy="changed")
    current=_snap(tmp_path)
    run,_=_runner("a"*40)
    result=analyze_rollback(current,older,"clean",runner=run)
    assert "PREREGISTRATION_OR_POLICY_IDENTITY_CHANGED" in result["blockers"]
    assert "SCHEMA_CHANGE_REQUIRES_MIGRATION_AND_RESTORE_PLAN" in result["blockers"]


def test_changed_effective_unit_blocks(tmp_path):
    older=_snap(tmp_path,sha="b"*40,units="old-unit")
    current=_snap(tmp_path)
    run,_=_runner("a"*40,["deploy/systemd/freequant-clean-shadow.service"])
    result=analyze_rollback(current,older,"clean",runner=run)
    assert "EFFECTIVE_SERVICE_OR_TEMPLATE_CHANGED" in result["blockers"]


def test_missing_candidate_git_commit_fails_closed(tmp_path):
    older=_snap(tmp_path,sha="b"*40)
    current=_snap(tmp_path)
    def fail_git(*args,**kwargs):
        return SimpleNamespace(returncode=1,stdout="",stderr="object missing")
    with pytest.raises(RollbackBlocked,match="Git metadata"):
        analyze_rollback(current,older,"clean",runner=fail_git)


def test_wrong_active_commit_blocks_even_if_baseline_matches(tmp_path):
    older=_snap(tmp_path,sha="b"*40)
    current=_snap(tmp_path)
    runner,_=_runner("e"*40)
    with pytest.raises(RollbackBlocked,match="changed since inspection"):
        analyze_rollback(current,older,"clean",runner=runner)


def test_invalid_manifest_checksum_rejected(tmp_path):
    report=_snap(tmp_path)
    report["identity"]["lanes"]["clean"]["git_sha"]="f"*40
    file=tmp_path/"approved.json"
    file.write_text(json.dumps(report))
    with pytest.raises(RollbackBlocked,match="altered"):
        _read_manifest(file)


def test_same_pinned_commit_is_noop_not_false_rollback(tmp_path):
    current=_snap(tmp_path)
    runner,_=_runner("a"*40)
    result=analyze_rollback(current,current,"clean",runner=runner)
    assert result["status"]=="NO_ROLLBACK_NEEDED"
    assert result["rollback_attempted"] is False
    assert result["service_restarted"] is False


def test_no_cli_side_effect_for_invalid_manifests(tmp_path,capsys):
    absent=tmp_path/"not-existent.json"
    assert main(["--current-baseline",str(absent),
                 "--target-baseline",str(absent),"--lane","clean"])==2
    error=json.loads(capsys.readouterr().err)
    assert error["status"]=="ROLLBACK_DRYRUN_FAIL_CLOSED"
    assert error["automatic_rollback"] is False


def test_unclassified_clean_runtime_module_is_protected(tmp_path):
    older=_snap(tmp_path,sha="b"*40)
    current=_snap(tmp_path)
    runner,_=_runner("a"*40,["quantlab/new_execution_logic.py"])
    result=analyze_rollback(current,older,"clean",runner=runner)
    assert result["status"]=="ROLLBACK_BLOCKED"
    assert "ACTIVE_CLEAN_COHORT_CONTRACT_CODE_CHANGED" in result["blockers"]


def test_non_ancestor_git_commit_does_not_count_as_rollback(tmp_path):
    older=_snap(tmp_path,sha="b"*40)
    current=_snap(tmp_path)
    def reject_nonancestor(args, **kwargs):
        command=args[3]
        if command=="merge-base":
            return SimpleNamespace(returncode=1,stdout="",stderr="")
        if command=="rev-parse":
            return SimpleNamespace(returncode=0,stdout="a"*40+"\n",stderr="")
        return SimpleNamespace(returncode=0,stdout="",stderr="")
    with pytest.raises(RollbackBlocked,match="Git metadata"):
        analyze_rollback(current,older,"clean",runner=reject_nonancestor)

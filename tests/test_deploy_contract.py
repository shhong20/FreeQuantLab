"""Offline-only tests for the deploy identity contract."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from quantlab.deploy_contract import (
    _fingerprint, _source, check_deployment, inspect_deployment, save_baseline,
)


def fixture(tmp_path):
    roots={"core":tmp_path/"core","clean":tmp_path/"clean"}
    for path in roots.values():
        (path/"deploy/systemd").mkdir(parents=True)
    installed=tmp_path/"installed"
    installed.mkdir()
    unit_data={}
    for unit,lane in (("core.service","core"),("clean.service","clean")):
        file_bytes=("[Service]\nExecStart=/bin/echo healthy\n").encode()
        (roots[lane]/"deploy/systemd"/unit).write_bytes(file_bytes)
        unit_path=installed/unit
        unit_path.write_bytes(file_bytes)
        unit_data[unit]=unit_path
    root_db=tmp_path/"state"
    root_db.mkdir()
    with sqlite3.connect(root_db/"shadow.db") as con:
        con.execute("CREATE TABLE demo(i INTEGER PRIMARY KEY)")
    return roots,unit_data,root_db


def source(roots,files,*,dirty=False,failed=False,altered_command=False):
    def runner(args, **kwargs):
        if args[0]=="git":
            if args[3:]==["rev-parse","HEAD"]:
                val=("a" if Path(args[2]).name=="core" else "b")*40
            else:
                val=" M evidence.json" if dirty else ""
            return SimpleNamespace(returncode=0,stdout=val+"\n" if val else "")
        unit=args[3]
        lane="core" if unit=="core.service" else "clean"
        argv="/bin/echo unexpected" if altered_command else "/bin/echo healthy"
        data=(f"WorkingDirectory={roots[lane]}\n"
              f"FragmentPath={files[unit]}\n"
              f"ExecStart={{ path=/bin/echo ; argv[]={argv} ; ignore_errors=no ; "
              f"start_time=[dynamic] ; status=2 }}\n"
              f"Result={'exit-code' if failed else 'success'}\n"
              f"ExecMainStatus={2 if failed else 0}\n")
        return SimpleNamespace(returncode=0,stdout=data)
    return runner


def snapshot(tmp_path,*,dirty=False,failed=False,altered_command=False):
    roots, units, state=fixture(tmp_path)
    report=inspect_deployment(
        roots=roots,services={"core.service":"core","clean.service":"clean"},
        evidence={"core":(),"clean":()},state={"core":("state/shadow.db",)},
        shared_state=state,
        runner=source(roots,units,dirty=dirty,failed=failed,
                      altered_command=altered_command),
    )
    return report,roots,units,state


def test_healthy_capture_verify_without_promotion(tmp_path):
    original,roots,units,state=snapshot(tmp_path)
    output=tmp_path/"baseline.json"
    save_baseline(output,original)
    assert json.loads(output.read_text())["identity_sha256"]==original["identity_sha256"]
    assert output.stat().st_mode & 0o077 == 0
    current=inspect_deployment(
        roots=roots,services={"core.service":"core","clean.service":"clean"},
        evidence={"core":(),"clean":()},state={"core":("state/shadow.db",)},
        shared_state=state,runner=source(roots,units))
    assert check_deployment(original,current)["status"]=="DEPLOYMENT_IDENTITY_MATCH"
    assert not original["safe_to_autodeploy"]
    assert not original["models_full_binary_sha256_verified"]
    with pytest.raises(FileExistsError):
        save_baseline(output,original)


def test_failed_audit_unit_is_not_release_config_drift(tmp_path):
    baseline,roots,units,state=snapshot(tmp_path)
    current=inspect_deployment(
        roots=roots,services={"core.service":"core","clean.service":"clean"},
        evidence={"core":(),"clean":()},state={"core":("state/shadow.db",)},
        shared_state=state,runner=source(roots,units,failed=True))
    assert check_deployment(baseline,current)["drift"]==[]
    assert current["observed_service_results"]["core.service"]["exit_code"]=="2"


def test_detect_unit_exec_start_tampering(tmp_path):
    baseline,roots,units,state=snapshot(tmp_path)
    current=inspect_deployment(
        roots=roots,services={"core.service":"core","clean.service":"clean"},
        evidence={"core":(),"clean":()},state={"core":("state/shadow.db",)},
        shared_state=state,runner=source(roots,units,altered_command=True))
    assert "SERVICE_DEFINITION_CHANGED:core.service" in check_deployment(baseline,current)["drift"]


def test_dirty_baseline_does_not_bless_worktree(tmp_path):
    baseline,_,_,_=snapshot(tmp_path,dirty=True)
    result=check_deployment(baseline,baseline)
    assert "DIRTY_RUNTIME:core" in result["drift"]


def test_source_unit_file_drift_is_detected(tmp_path):
    baseline,roots,units,state=snapshot(tmp_path)
    units["clean.service"].write_text("[Service]\nExecStart=/bin/true\n")
    current=inspect_deployment(
        roots=roots,services={"core.service":"core","clean.service":"clean"},
        evidence={"core":(),"clean":()},state={"core":("state/shadow.db",)},
        shared_state=state,runner=source(roots,units))
    report=check_deployment(baseline,current)
    assert "UNIT_TEMPLATE_DRIFT:clean.service" in report["drift"]


def test_manifest_fingerprint_rejects_serialized_mutation(tmp_path):
    baseline,_,_,_=snapshot(tmp_path)
    corrupt=deepcopy(baseline)
    corrupt["identity"]["lanes"]["core"]["git_sha"]="0"*40
    with pytest.raises(ValueError,match="fingerprint"):
        check_deployment(corrupt,baseline)


def test_argv_ignore_runtime_pid():
    a="ExecStart={ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; start_time=[now] ; pid=123 }"
    b=a.replace("now","yesterday").replace("123","8888")
    assert _source(a)==_source(b)

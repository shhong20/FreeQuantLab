"""Explicit pinned SSH transfer, no ambient keys, unknown host or silent writes."""
from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from quantlab.websocket_ssh_transport import (
    load_profile,preflight,transfer,_common_ssh
)
from test_websocket_remote_backup import fixture as archive_fixture


def profile(tmp_path):
    key=tmp_path/"id_remote"
    key.write_text("mock identity; real SSH still validates signature")
    pinned=tmp_path/"known_hosts"
    pinned.write_text("backup.example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBOGUSKEY\n")
    conf={
        "host":"backup.example.test","user":"backup",
        "port":22,"identity_file":str(key),
        "known_hosts_file":str(pinned),
        "remote_inbox_parent":"/srv/quantlab/incoming",
        "remote_archive_parent":"/srv/quantlab/archives",
        "remote_python":"/opt/quantlab/bin/python3",
    }
    config=tmp_path/"remote.json"
    config.write_text(json.dumps(conf))
    return config,conf


def test_missing_pinned_host_key_fails_before_ssh(tmp_path):
    config,profile_data=profile(tmp_path)
    Path(profile_data["known_hosts_file"]).write_text("")
    with pytest.raises(ValueError,match="host pin|known_hosts"):
        load_profile(config)


def test_disallow_unsafe_shell_paths_hosts_and_remote_users(tmp_path):
    config,data=profile(tmp_path)
    for field,value in (
        ("host","evil.example.test;touch /tmp/leak"),
        ("user","root;echo"),
        ("remote_archive_parent","/srv/quantlab/../../root"),
        ("remote_inbox_parent","/tmp"),
        ("remote_python","python3;id"),
        ("port","22"),
    ):
        wrong=dict(data);wrong[field]=value
        config.write_text(json.dumps(wrong))
        with pytest.raises(ValueError):
            load_profile(config)


def test_known_hosts_requires_exact_nondefault_port(tmp_path):
    config,data=profile(tmp_path)
    data["port"]=22222
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError,match="host:port"):
        load_profile(config)
    Path(data["known_hosts_file"]).write_text(
        "[backup.example.test]:22222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE\n"
    )
    assert load_profile(config)["port"]==22222


def test_readonly_preflight_uses_strict_known_hosts_and_no_agent_forwarding(tmp_path):
    config,data=profile(tmp_path)
    calls=[]
    def mocked(args,**kwargs):
        calls.append(args)
        return CompletedProcess(args,0,json.dumps({
            "status":"EMPTY_RECEIVER_NOT_YET_PROTECTED",
            "segment_count":0,"last_manifest_sha256":None}),"")
    out=preflight(profile_path=config,runner=mocked)
    assert out["status"]=="PINNED_HOST_AND_REMOTE_RECEIVER_PREFLIGHT_PASS"
    assert not out["real_remote_transfer_performed"]
    assert len(calls)==1 and calls[0][0]=="ssh"
    assert "-F" in calls[0] and "/dev/null" in calls[0]
    assert "StrictHostKeyChecking=yes" in calls[0]
    assert "ForwardAgent=no" in calls[0]
    assert "BatchMode=yes" in calls[0]
    assert "--status" in calls[0][-1] and "--receive" not in calls[0][-1]


def test_bad_ssh_proof_is_not_silently_accepted(tmp_path):
    config,data=profile(tmp_path)
    def rejected(args,**kwargs):
        return CompletedProcess(args,255,"","Host key verification failed")
    with pytest.raises(ConnectionError,match="Pinned SSH command failed"):
        preflight(profile_path=config,runner=rejected)
    def bad(args,**kwargs):
        return CompletedProcess(args,0,'{"status":"YEAH_BACKUP_OK","segment_count":0}',"")
    with pytest.raises(ValueError,match="receiver"):
        preflight(profile_path=config,runner=bad)


def test_transfer_invalid_remote_prefix_prevents_scp(tmp_path):
    # First mock only the remote status; no file writes/transport.
    config,data=profile(tmp_path)
    source=tmp_path/"source"
    source.mkdir()
    calls=[]
    def fake(args,**kwargs):
        calls.append(args)
        return CompletedProcess(args,0,json.dumps({
            "status":"RECEIVER_PREFIX_SEALED_NOT_FULLY_RESTORED_NOW",
            "segment_count":99,"last_manifest_sha256":"0"*64
        }),"")
    with pytest.raises(ValueError,match="ahead"):
        transfer(profile_path=config,source_root=source,outbox_parent=tmp_path,
                 runner=fake)
    assert len(calls)==1 and calls[0][0]=="ssh"


def test_ssh_profile_never_uses_ambient_config_or_passwords(tmp_path):
    config,data=profile(tmp_path)
    parsed=load_profile(config)
    options=_common_ssh(parsed)
    for opt in ("BatchMode=yes","PasswordAuthentication=no",
                "KbdInteractiveAuthentication=no","UpdateHostKeys=no",
                "StrictHostKeyChecking=yes","ClearAllForwardings=yes",
                "ForwardAgent=no","IdentitiesOnly=yes"):
        assert opt in options
    assert "-F" in options and "/dev/null" in options
    assert parsed["remote_archive_parent"]!="/"


def test_simulated_ssh_transfer_calls_receiver_full_sqlite_restore(tmp_path):
    import shutil
    from quantlab.websocket_remote_backup import accept_bundle,receiver_status
    from quantlab.websocket_incremental_archive import add_segment
    from test_websocket_archive import add_session
    from test_websocket_incremental_archive import TODAY
    from quantlab.websocket_ssh_transport import transfer

    config,data=profile(tmp_path)
    live=tmp_path/"producer.db"
    add_session(live,number=0)
    add_session(live,number=1)
    source=tmp_path/"archives"
    for i in range(2):
        add_segment(live_db=live,root=source,through_date=TODAY,max_sessions=1)
    outbox=tmp_path/"outbox"
    outbox.mkdir()
    simulated_destination=tmp_path/"separately-isolated-recipient"
    simulated_inbox=tmp_path/"isolated-inbox"
    simulated_inbox.mkdir()
    calls=[]
    def fake_runner(args,**kwargs):
        calls.append(args)
        if args[0]=="scp":
            local=Path(args[-2])
            shutil.copytree(local,simulated_inbox/local.name)
            return CompletedProcess(args,0,"","")
        if "--status" in args[-1]:
            result=receiver_status(simulated_destination)
        elif "--receive" in args[-1]:
            incoming=simulated_inbox/next(iter(simulated_inbox.iterdir())).name
            result=accept_bundle(bundle_dir=incoming,target_root=simulated_destination)
        else:
            raise AssertionError(args)
        return CompletedProcess(args,0,json.dumps(result),"")
    result=transfer(profile_path=config,source_root=source,
                    outbox_parent=outbox,runner=fake_runner)
    assert result["status"]=="REMOTE_PINNED_SSH_TRANSFER_AND_RECEIVER_FULL_RESTORE_REPORTED"
    assert result["remote_frames"]==14 and result["remote_sessions"]==2
    assert result["transferred_segments"]==2
    assert result["receiver_reported_full_sqlite_restore"]
    assert [x[0] for x in calls]==["ssh","scp","ssh"]
    # Uses safe SFTP default, not legacy scp remote shell (-O).
    assert "-O" not in calls[1]
    assert "-P" in calls[1] and "StrictHostKeyChecking=yes" in calls[1]
    # The actual fake transfer is local isolated storage: never claim it is
    # physically independent hardware.
    assert result["different_physical_hardware_not_attested_by_ssh"]

def test_receiver_forged_success_counts_are_rejected(tmp_path):
    import shutil
    from quantlab.websocket_remote_backup import receiver_status
    from test_websocket_archive import add_session
    from quantlab.websocket_incremental_archive import add_segment
    from test_websocket_incremental_archive import TODAY

    cfg,data=profile(tmp_path)
    live=tmp_path/"source.sqlite"
    add_session(live)
    local=tmp_path/"incremental"
    add_segment(live_db=live,root=local,through_date=TODAY,max_sessions=1)
    outbox=tmp_path/"outbox"
    outbox.mkdir()
    calls=[]
    def fraud(args,**kwargs):
        calls.append(args)
        if args[0]=="scp":
            return CompletedProcess(args,0,"","")
        if "--status" in args[-1]:
            answer={"status":"EMPTY_RECEIVER_NOT_YET_PROTECTED",
                    "segment_count":0,"last_manifest_sha256":None}
        else:
            # Fake "success" but knowingly incorrect recovered count.
            answer={"status":"RECEIVER_INDEPENDENT_SQLITE_RESTORED_AND_VERIFIED",
                    "raw_SHA256_all_frames_verified":True,
                    "archived_segments":1,"frames":9999,"sessions":1}
        return CompletedProcess(args,0,json.dumps(answer),"")
    with pytest.raises(ValueError,match="full SQLite recovery proof"):
        transfer(profile_path=cfg,source_root=local,
                 outbox_parent=outbox,runner=fraud)
    assert len(calls)==3


def test_receiver_rejects_source_provenance_forgery(tmp_path):
    from quantlab.websocket_remote_backup import create_bundle,accept_bundle
    from quantlab.websocket_incremental_archive import add_segment
    from test_websocket_archive import add_session
    from test_websocket_incremental_archive import TODAY
    live=tmp_path/"source.sqlite"
    add_session(live)
    source=tmp_path/"evidence"
    add_segment(live_db=live,root=source,through_date=TODAY)
    target=tmp_path/"receiving"
    box=tmp_path/"first-batch"
    create_bundle(source_root=source,outbox=box,base_segment_count=0)
    accept_bundle(bundle_dir=box,target_root=target)
    # Existing target segments must never be silently linked to an archive
    # from a different producer. Different source manifests still reject.
    doc=box/"transfer.json"
    record=json.loads(doc.read_text())
    record["source_db"]="/another/producer.sqlite"
    doc.write_text(json.dumps(record))
    with pytest.raises(ValueError,match="provenance"):
        accept_bundle(bundle_dir=box,target_root=target)

"""Explicit opt-in, pinned-host-key SSH/SFTP transport for PUBLIC WS evidence.

Source creates a bounded verified bundle; only after real SSH verification
against an explicitly configured host and path may it be transmitted. Remote
side must install the same quantlab.websocket_remote_backup receiver and
pre-provision an inbox parent + archive parent with restricted permissions.

Never disables host-key checks, enables agent forwarding, invents credentials
or guesses another computer's address. No unattended timer, automatic delete,
live orders, or claims of offsite proof before a real remote restore succeeds.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Callable

from .websocket_archive import _sha
from .websocket_incremental_archive import _read_chain,MANIFEST
from .websocket_remote_backup import create_bundle

ADDRESS=re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}$")
USERNAME=re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
REMOTE_PATH=re.compile(r"^/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*$")
LOCAL_RUN:Callable=subprocess.run


def _path_valid(s:str)->bool:
    return bool(REMOTE_PATH.fullmatch(s)) and ".." not in s.split("/") and "//" not in s


def load_profile(profile_path:str|Path)->dict:
    cfg=json.loads(Path(profile_path).read_text("utf-8"))
    expected={"host","user","port","identity_file","known_hosts_file",
              "remote_inbox_parent","remote_archive_parent","remote_python"}
    if set(cfg)!=expected:
        raise ValueError("Explicit SSH profile fields mismatch")
    if (not isinstance(cfg["host"],str) or not ADDRESS.fullmatch(cfg["host"]) or
        not isinstance(cfg["user"],str) or not USERNAME.fullmatch(cfg["user"]) or
        type(cfg["port"]) is not int or not 1<=cfg["port"]<=65535):
        raise ValueError("Invalid host, SSH user or port")
    for p in ("remote_inbox_parent","remote_archive_parent"):
        val=cfg[p]
        if not isinstance(val,str) or not _path_valid(val) or val in ("/","/tmp"):
            raise ValueError("Invalid remote pre-provisioned path")
    if cfg["remote_inbox_parent"]==cfg["remote_archive_parent"]:
        raise ValueError("Inbox and protected receiver archive must be separate")
    interpreter=cfg["remote_python"]
    if (not isinstance(interpreter,str) or
        not (interpreter=="python3" or
             (_path_valid(interpreter) and
              interpreter.rsplit("/",1)[-1] in ("python","python3")))):
        raise ValueError("Remote Python must be explicitly pinned to a safe executable path")
    for field in ("identity_file","known_hosts_file"):
        value=cfg[field]
        if not isinstance(value,str) or not value.startswith("/"):
            raise ValueError("SSH key/known_hosts path must be absolute")
        path=Path(value)
        if path.is_symlink() or not path.is_file() or path.stat().st_size<=0:
            raise ValueError("Missing or symlink SSH identity/host pin")
    # known_hosts is locally pinned. Do NOT auto-scan ssh-keyscan and blindly
    # trust a network-provided key.
    hosts=Path(cfg["known_hosts_file"]).read_text("utf-8")
    key_target=(cfg["host"] if cfg["port"]==22 else
                f'[{cfg["host"]}]:{cfg["port"]}')
    if not any(key_target in row.split()[0].split(",")
               for row in hosts.splitlines()
               if row.strip() and not row.startswith("#") and len(row.split())>=3):
        raise ValueError("Pinned known_hosts has no exact requested remote host:port")
    return cfg


def _common_ssh(cfg:dict)->list[str]:
    return [
        "-F","/dev/null",
        "-i",cfg["identity_file"],
        "-p",str(cfg["port"]),
        "-o","BatchMode=yes",
        "-o","StrictHostKeyChecking=yes",
        "-o","UserKnownHostsFile="+cfg["known_hosts_file"],
        "-o","GlobalKnownHostsFile=/dev/null",
        "-o","IdentitiesOnly=yes",
        "-o","ForwardAgent=no",
        "-o","ClearAllForwardings=yes",
        "-o","ConnectTimeout=8",
        "-o","ConnectionAttempts=1",
        "-o","PasswordAuthentication=no",
        "-o","KbdInteractiveAuthentication=no",
        "-o","UpdateHostKeys=no",
        "-o","ControlMaster=no",
    ]


def _exec_ssh(cfg,remote_command,runner=LOCAL_RUN):
    host=f'{cfg["user"]}@{cfg["host"]}'
    args=["ssh",*_common_ssh(cfg),"-T",host,remote_command]
    result=runner(args,capture_output=True,text=True,timeout=45,check=False)
    if result.returncode!=0:
        raise ConnectionError("Pinned SSH command failed: "+result.stderr[-350:])
    try:return json.loads(result.stdout)
    except ValueError as exc:raise ValueError("Remote returned no JSON proof") from exc


def _remote_cmd(cfg,mode:str,*,incoming:str|None=None)->str:
    # Profile paths are restricted to unambiguous path characters, and all
    # argv words quoted before being passed to ssh's remote POSIX shell.
    args=[cfg["remote_python"],"-m","quantlab.websocket_remote_backup"]
    if mode=="status":
        args.extend(["--status","--target",cfg["remote_archive_parent"]+"/websocket-incremental"])
    elif mode=="receive":
        if incoming is None or not _path_valid(incoming):
            raise ValueError("Unsafe remote incoming path")
        args.extend(["--receive","--incoming",incoming,
                     "--target",cfg["remote_archive_parent"]+"/websocket-incremental"])
    else:raise ValueError("Unsupported remote command")
    return " ".join(shlex.quote(a) for a in args)


def preflight(*,profile_path:str|Path,runner=LOCAL_RUN)->dict:
    cfg=load_profile(profile_path)
    result=_exec_ssh(cfg,_remote_cmd(cfg,"status"),runner)
    if result.get("status") not in (
        "EMPTY_RECEIVER_NOT_YET_PROTECTED",
        "RECEIVER_PREFIX_SEALED_NOT_FULLY_RESTORED_NOW",
    ):
        raise ValueError("Remote receiver did not return expected read-only prefix")
    count=result.get("segment_count")
    if type(count) is not int or count<0:
        raise ValueError("Invalid remote prefix count")
    if count and not isinstance(result.get("last_manifest_sha256"),str):
        raise ValueError("Remote prefix lacks SHA head")
    return {"status":"PINNED_HOST_AND_REMOTE_RECEIVER_PREFLIGHT_PASS",
            "remote":cfg["host"],"remote_user":cfg["user"],
            "remote_segment_count":count,"remote_manifest_head":result["last_manifest_sha256"],
            "remote_full_restore_performed":False,
            "real_remote_transfer_performed":False}


def transfer(
    *,profile_path:str|Path,source_root:str|Path,outbox_parent:str|Path,
    runner=LOCAL_RUN,
)->dict:
    cfg=load_profile(profile_path)
    source=Path(source_root).resolve()
    outbox=Path(outbox_parent).resolve()
    if not outbox.is_dir() or source==outbox or source in outbox.parents:
        raise ValueError("Transfer outbox must be existing and outside the source")
    before=preflight(profile_path=profile_path,runner=runner)
    segments,docs=_read_chain(source)
    base=before["remote_segment_count"]
    if base>len(segments):
        raise ValueError("Remote receiver ahead of source")
    if base and _sha(segments[base-1]/MANIFEST)!=before["remote_manifest_head"]:
        raise ValueError("Pinned remote prefix differs from local source")
    if base==len(segments):
        return {"status":"REMOTE_ALREADY_HAS_CURRENT_SOURCE_PREFIX",
                "remote":cfg["host"],"transferred_segments":0,
                "real_remote_transfer_performed":False}
    import uuid
    batch_id="bundle-"+uuid.uuid4().hex[:20]
    local=outbox/batch_id
    result=create_bundle(source_root=source,outbox=local,base_segment_count=base)
    if result["status"]!="INDEPENDENT_TRANSPORT_BUNDLE_SEALED":
        raise ValueError("Cannot build authenticated source bundle")
    remote_incoming=cfg["remote_inbox_parent"]+"/"+batch_id
    # Preprovisioned remote inbox parent + SFTP subsystem required. Copy
    # bundle directory as new unique basename, never overwrite old data.
    host=f'{cfg["user"]}@{cfg["host"]}'
    ssh_options=_common_ssh(cfg)
    # scp uses -P (uppercase) for port, ssh uses -p. No shell command
    # string is interpolated from the source or bundle contents.
    options=list(ssh_options)
    ix=options.index("-p")
    options[ix]="-P"
    command=["scp",*options,"-r",str(local),host+":"+cfg["remote_inbox_parent"]+"/"]
    copy=runner(command,capture_output=True,text=True,timeout=120,check=False)
    if copy.returncode!=0:
        raise ConnectionError("Pinned SFTP transfer failed: "+copy.stderr[-350:])
    receipt=_exec_ssh(cfg,_remote_cmd(cfg,"receive",incoming=remote_incoming),runner)
    final_count=base+result["exported_segments"]
    expected_frames=sum(x["frames"] for x in docs[:final_count])
    expected_sessions=sum(x["sessions"] for x in docs[:final_count])
    if (receipt.get("status")!="RECEIVER_INDEPENDENT_SQLITE_RESTORED_AND_VERIFIED"
        or receipt.get("raw_SHA256_all_frames_verified") is not True
        or receipt.get("archived_segments")!=final_count
        or receipt.get("frames")!=expected_frames
        or receipt.get("sessions")!=expected_sessions):
        raise ValueError("Remote did not return valid full SQLite recovery proof")
    return {
        "status":"REMOTE_PINNED_SSH_TRANSFER_AND_RECEIVER_FULL_RESTORE_REPORTED",
        "remote":cfg["host"],"remote_user":cfg["user"],
        "remote_archive_path":cfg["remote_archive_parent"]+"/websocket-incremental",
        "segments":receipt["archived_segments"],
        "remote_result_counts_match_local_archive_manifest":True,
        "transferred_segments":result["exported_segments"],
        "remote_frames":receipt["frames"],
        "remote_sessions":receipt["sessions"],
        "real_remote_transfer_performed":True,
        "receiver_reported_full_sqlite_restore":True,
        "remote_host_integrity_beyond_ssh_pinning_not_proven":True,
        "different_physical_hardware_not_attested_by_ssh":True,
        "source_original_data_deleted":False,
        "real_orders_enabled":False,
    }


def main(argv=None):
    p=argparse.ArgumentParser(description="Explicit pinned-SSH public WS segment transfer")
    p.add_argument("--profile",required=True,help="User-provisioned host, SSH identity, pinned known_hosts and remote paths")
    p.add_argument("--source",default="/home/hong/apps/FreeQuantLab/state/microstructure/incremental")
    p.add_argument("--outbox-parent",default="/home/hong/apps/FreeQuantLab/state/microstructure/transfer-outbox")
    p.add_argument("--transfer",action="store_true",help="Requires explicit intent; otherwise read-only preflight")
    args=p.parse_args(argv)
    result=(transfer(profile_path=args.profile,source_root=args.source,
                     outbox_parent=args.outbox_parent)
            if args.transfer else preflight(profile_path=args.profile))
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

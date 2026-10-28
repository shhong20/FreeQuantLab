"""Remove only the four named one-day FreeQuantLab KRX cron entries.

Invoked after the 2026-10-29 pilot market closeout. Never edits arbitrary
crontab jobs, refuses execution before the actual session, and preserves
non-pilot cron entries byte-for-byte.
"""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import subprocess
from zoneinfo import ZoneInfo

TAG="FREEQUANTLAB_KRX_JOINT_PILOT_20261029"
ROOT="/home/hong/apps/FreeQuantLab/state/intraday_research/krx_joint_pilot_20261029_r1"
RUN="/home/hong/apps/FreeQuantLab-krx-failure-20261028/ops/krx_joint_pilot_20261029.sh"
CLOSE="/home/hong/apps/FreeQuantLab-krx-failure-20261028/ops/krx_joint_pilot_finalize_resumable_20261029.sh"
CATCH="/home/hong/apps/FreeQuantLab-krx-failure-20261028/ops/krx_pilot_missed_closeout_watch_20261029.sh"
EXPECTED=(
    f"# {TAG} (one calendar date only; needs running WSL cron)",
    f"43 9 29 10 * /bin/bash {RUN} morning >> {ROOT}/morning.log 2>&1 # {TAG}",
    f"35 12 29 10 * /bin/bash {RUN} midday >> {ROOT}/midday.log 2>&1 # {TAG}",
    f"35 14 29 10 * /bin/bash {RUN} afternoon >> {ROOT}/afternoon.log 2>&1 # {TAG}",
    f"45 15 29 10 * /bin/bash {CLOSE} >> {ROOT}/finalize.log 2>&1 # {TAG}",
    f"0 * * * * /bin/bash {CATCH} >> {ROOT}/catchup.log 2>&1 # {TAG}",
)


def remove_pilot_entries(cron:str,now:datetime) -> str:
    local=now.astimezone(ZoneInfo("Asia/Seoul"))
    boundary=datetime(2026,10,29,15,45,tzinfo=ZoneInfo("Asia/Seoul"))
    if local<boundary:
        raise ValueError("KRX pilot cleanup is allowed only after 2026-10-29 15:45 KST")
    # Also permit *later* catch-up after suspended WSL resumes. Exact
    # cron-entry matching prevents removing any unrelated scheduled job.
    lines=cron.splitlines()
    marked=[line for line in lines if TAG in line]
    if len(marked)!=len(EXPECTED) or set(marked)!=set(EXPECTED):
        raise ValueError("Pilot cron entries changed; refuse automatic removal")
    result="\n".join(line for line in lines if line not in EXPECTED)
    return result+"\n" if result else ""


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description="Strict one-day KRX research cron cleanup")
    p.add_argument("--execute",action="store_true")
    args=p.parse_args(argv)
    if not args.execute:
        p.exit(2,"Only --execute supports the guarded cleanup\n")
    try:
        current=subprocess.run(["crontab","-l"],capture_output=True,
                               text=True,check=True)
        updated=remove_pilot_entries(current.stdout,datetime.now(timezone.utc))
        done=subprocess.run(["crontab","-"],input=updated,
                            text=True,capture_output=True)
        if done.returncode!=0:raise ValueError("crontab update failed")
        check=subprocess.run(["crontab","-l"],capture_output=True,text=True)
        if check.returncode not in (0,1) or TAG in check.stdout:
            raise ValueError("crontab cleanup not confirmed")
        print("KRX_ONE_DAY_CRON_CLEANUP_SUCCESS")
        return 0
    except (ValueError,OSError,subprocess.CalledProcessError):
        print("KRX_ONE_DAY_CRON_CLEANUP_NOT_VERIFIED")
        return 2


if __name__=="__main__":
    raise SystemExit(main())

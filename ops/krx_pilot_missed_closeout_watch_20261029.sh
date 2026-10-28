#!/usr/bin/env bash
# Hourly, NO-NETWORK date gate. Runs only after original KRX pilot closeout;
# WSL suspension simply means the first subsequent active cron hour.
set -euo pipefail
umask 077
STAMP="$(TZ=Asia/Seoul date +%Y%m%d%H%M)"
if [[ "$STAMP" < "202610291545" ]]; then
  exit 0
fi
exec /bin/bash /home/hong/apps/FreeQuantLab-krx-resume-20261028/ops/krx_joint_pilot_finalize_resumable_20261029.sh

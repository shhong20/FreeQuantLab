#!/usr/bin/env bash
# 2026-10-29 KRX market-data pilot closeout, offline only.
# A partial/missing result remains failed. Cleanup is still attempted.
set -uo pipefail
umask 077
cd /home/hong/apps/FreeQuantLab-krx-closeout-20261028 || exit 2
P=/home/hong/apps/FreeQuantLab/.venv/bin/python
"$P" -m quantlab.intraday_krx_joint_pilot_review
review_rc=$?
"$P" ops/krx_pilot_cron_cleanup_20261029.py --execute
cleanup_rc=$?
if [ "$review_rc" -ne 0 ] || [ "$cleanup_rc" -ne 0 ]; then
    echo "KRX_RESEARCH_PILOT_CLOSEOUT_INCOMPLETE"
    exit 1
fi
echo "KRX_RESEARCH_PILOT_CLOSEOUT_VERIFIED"

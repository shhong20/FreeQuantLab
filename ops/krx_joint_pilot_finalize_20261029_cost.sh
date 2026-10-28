#!/usr/bin/env bash
# 2026-10-29 KRX-J one-day market pilot closeout + offline cost analysis.
# Runs only from WSL cron 15:45 KST; never issues orders or KIS HTTP.
set -uo pipefail
umask 077
cd /home/hong/apps/FreeQuantLab-krx-cost-20261028 || exit 2
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research
PILOT="$ROOT/krx_joint_pilot_20261029_r1"
FINAL="$PILOT/2026-10-29_final_review"
COST="$PILOT/2026-10-29_cost_sensitivity"

"$PY" -m quantlab.intraday_krx_joint_pilot_review
review_rc=$?

if [ -d "$FINAL" ]; then
    if [ -d "$COST" ]; then
        "$PY" -c 'from pathlib import Path;from hashlib import sha256;p=Path(__import__("sys").argv[1]); d=sha256((p/"cost_sensitivity.json").read_bytes()).hexdigest();assert d==(p/"cost_sensitivity.sha256").read_text().strip()' "$COST"
        cost_rc=$?
    else
        "$PY" -m quantlab.intraday_krx_dynamic_cost \
          --l2-root "$ROOT/krx_live_l2_20261028_r2" \
          --pilot-root "$PILOT" \
          --pit-dir "$ROOT/krx100_pit_replay_20261028_r3" \
          --output-dir "$COST"
        cost_rc=$?
    fi
else
    cost_rc=1
    echo "KRX_COST_AUDIT_NOT_RUN_WITHOUT_VERIFIED_PILOT_FINAL_REVIEW"
fi

# Delete ONLY the four exactly matched 2026-10-29 research reservations.
# Cleanup still executes if the market data collection/compare failed.
"$PY" ops/krx_pilot_cron_cleanup_20261029.py --execute
cleanup_rc=$?

if [ "$review_rc" -ne 0 ] || [ "$cost_rc" -ne 0 ] || [ "$cleanup_rc" -ne 0 ]; then
    echo "KRX_JOINT_PILOT_RESEARCH_CLOSEOUT_PARTIAL_OR_FAILED"
    exit 1
fi
echo "KRX_JOINT_PILOT_RESEARCH_CLOSEOUT_AND_COST_SUCCESS"

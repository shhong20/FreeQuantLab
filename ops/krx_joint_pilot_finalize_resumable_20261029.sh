#!/usr/bin/env bash
# Read-only KRX 2026-10-29 pilot final review; safe if WSL resumes after date.
# No KIS API or broker activity. One finalizer process; immutable result.
set -uo pipefail
umask 077
cd /home/hong/apps/FreeQuantLab-krx-failure-20261028 || exit 2
PY=/home/hong/apps/FreeQuantLab/.venv/bin/python
ROOT=/home/hong/apps/FreeQuantLab/state/intraday_research
PILOT="$ROOT/krx_joint_pilot_20261029_r1"
FINAL="$PILOT/2026-10-29_final_review"
COST="$PILOT/2026-10-29_cost_sensitivity"

# Reject manual or cron invocation before the true KRX pilot closeout.
STAMP="$(TZ=Asia/Seoul date +%Y%m%d%H%M)"
if [[ "$STAMP" < "202610291545" ]]; then
  echo "KRX_CLOSEOUT_NOT_DUE_NO_ACTION"
  exit 0
fi

# The 15:45 cron and hourly catch-up must not race or overwrite immutable data.
exec 9>"$PILOT/.finalize.lock" || exit 2
flock -n 9 || { echo "KRX_CLOSEOUT_ALREADY_IN_PROGRESS"; exit 0; }

if [ -e "$FINAL" ]; then
    "$PY" - "$FINAL" <<'PY'
from pathlib import Path
from hashlib import sha256
import json,sys
p=Path(sys.argv[1])
try:
    b=(p/"final_review.json").read_bytes()
    expected=(p/"final_review.sha256").read_text().strip()
    assert sha256(b).hexdigest()==expected
    d=json.loads(b)
    assert d["session"]=="2026-10-29" and d["broker_orders"]==0
    assert d["backtesting_approval"] is False
    assert d["paper_or_live_approval"] is False
    print("KRX_IMMUTABLE_FINAL_REVIEW",d["status"])
    sys.exit(0 if d["verified_windows"]==3 else 1)
except (OSError,ValueError,KeyError,AssertionError):
    print("KRX_EXISTING_FINAL_REVIEW_INVALID")
    sys.exit(2)
PY
    review_rc=$?
else
    "$PY" -m quantlab.intraday_krx_joint_pilot_review
    review_rc=$?
fi

# Compare the new session's source-verified depth only. Never reconstruct
# a missed/failed quote; older reference quotes remain in their own group.
if [ "$review_rc" -le 1 ] && [ -d "$FINAL" ]; then
    if [ -d "$COST" ]; then
        "$PY" - "$COST" <<'PY'
from pathlib import Path
from hashlib import sha256
import json,sys
try:
    p=Path(sys.argv[1])
    b=(p/"cost_sensitivity.json").read_bytes()
    assert sha256(b).hexdigest()==(p/"cost_sensitivity.sha256").read_text().strip()
    d=json.loads(b)
    assert d["actual_broker_fill_records"]==0 and d["strategy_returns"] is None
    assert d["eligible_for_live_or_paper_trading"] is False
    print("KRX_EXISTING_COST_REPORT_VERIFIED")
except (OSError,ValueError,KeyError,AssertionError):
    print("KRX_EXISTING_COST_REPORT_INVALID")
    sys.exit(2)
PY
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
    cost_rc=2
    echo "KRX_COST_REPORT_SKIPPED_UNVERIFIED_FINAL_REVIEW"
fi

# Remove only the exactly known 2026-10-29 jobs (including the catch-up
# checker). Cleanup also happens if the market data was never captured.
"$PY" ops/krx_pilot_cron_cleanup_20261029.py --execute
cleanup_rc=$?

if [ "$review_rc" -ne 0 ] || [ "$cost_rc" -ne 0 ] || [ "$cleanup_rc" -ne 0 ]; then
    echo "KRX_CLOSEOUT_PARTIAL_OR_FAILED_NO_FILLS_CERTIFIED"
    exit 1
fi
echo "KRX_CLOSEOUT_VERIFIED_RESEARCH_ONLY_NO_FILLS"

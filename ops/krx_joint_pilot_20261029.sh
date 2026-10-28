#!/usr/bin/env bash
# One-day, non-trading KRX research pilot. No credentials on command line.
set -euo pipefail
umask 077
case "${1:-}" in
  morning|midday|afternoon) ;;
  *) exit 2 ;;
esac
cd /home/hong/apps/FreeQuantLab-krx-pilot-20261029
exec /home/hong/apps/FreeQuantLab/.venv/bin/python \
    -m quantlab.intraday_krx_joint_pilot \
    --session 2026-10-29 --window "$1" --execute

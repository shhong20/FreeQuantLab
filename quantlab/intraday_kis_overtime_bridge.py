"""Read-only CLI: inspect distinct overtime daily volume alongside KRX daily-minute gaps."""
from __future__ import annotations

from .intraday_kis_overtime_daily import compare_main

if __name__ == "__main__":
    raise SystemExit(compare_main())

# FreeQuantLab — WebSocket collection quality and storage audit (2026-10-21)

## Status and boundaries

This is a read-only quality gate for PUBLIC Upbit 30-level orderbooks and market trade prints. No exchange orders, customer trade execution events, API keys, strategy promotion, automatic portfolio switching, or backfilled historical L2 data are used. The existing 10-minute REST observer and baseline/Exaone/Qlib paper ledgers are unchanged.

The WebSocket is intentionally sampled only 30 seconds every 600 seconds. Even with perfect timer attendance this is **5% duty cycle**, not a continuous full-day tape. A trade printed in the public feed is not a fill of our own order.

## Actual WSL observational evidence

The first four operational WS sessions on 2026-10-21 yielded:
- Four clean sessions, 3 distinct 10-minute wall-time slots (the second session was started at timer installation), 0 reconnects, failed frames or observed duplicates.
- About 5.93 MB of raw public WebSocket payload and 8.68 MB of SQLite main/WAL/SHM combined footprint.
- Rough disk growth extrapolation **313 MB/day** or **4.38 GB/14 days**, assuming the short initial capture sample is representative. This is a rough forecast, not a throughput guarantee; rate is recalculated as subsequent sessions arrive.
- Per-asset median quote receipt-minus-exchange timestamps of 30–32 ms, P95 39–42 ms. Exchange/server clock skew is included. This is neither broker-order latency nor confirmed execution time.
- KRW-ADA had no public trade messages during the first 3 sampling windows and 4 messages by the fourth window. This is **insufficient tape coverage**, not proof of no ADA trading.

## New research-only analyzer

Source: quantlab/websocket_quality.py.

- Reads the recent 24h session/frame metadata via indexed SQLite joins, without loading the full raw JSON payloads into memory. Checks database structure with SQLite quick_check. Raw SHA and causal orderbook/trade join authenticity remain in the separate cryptographic audit job; the quality report explicitly states this separation.
- Counts at most ONE successful complete session per 10-minute scheduled slot. Missing capture slots, longest consecutive outage, sessions attempted, rejected frames, reconnects, duplicated public-trade IDs and last good timestamp are reported independently.
- Reports BTC/ETH/XRP/ADA visible 30-level books and public prints, number of distinct exchange book timestamps, per-asset quote receipt minus exchange delay P50/P95/max, limited trade sample flags, public print to earlier-book references.
- Allows provisional 24h-window PASS only after **actual 24h elapsed** and at least 90% of 144 scheduled slots, no more than 3 consecutive missed slots, at least 100 distinct per-symbol exchange book timestamps, fresh clean session within 40 min, and quote-age P95 <= 3 seconds per market. This gate means adequate sampled public quote evidence only; it does NOT establish order-book queue availability or market-impact parameters for our own trades.
- Warns when forecast daily database storage exceeds **1 GiB/day** or actual SQLite+WAL+SHM exceeds **10 GiB**. An enforce run exits 2 on these storage warnings, even before 24h. It never deletes archived source bytes.

## Operational raw-audit performance guard

The initial WebSocket auditor read the entire 48h raw JSON into memory every 30 minutes. That risks exceeding the service timeout once throughput grows. The updated freequant-websocket-audit.service now runs sequentially:

1. Websocket raw SHA/causal-link audit for the most recent **2 hours**, plus clean-session freshness threshold of 2400 seconds.
2. A SQL metadata-based rolling 24h quality and disk-budget audit (with --enforce).

The service timeout increases from 60 to 90 seconds. **Raw frames older than 2h are not cryptographically revalidated on each 30-minute timer**. Operators may explicitly run the slower full-48-hour source audit independently; no older evidence is silently declared rehashed.

## Verification

Regression tests include 145 synthetic successful 10-min slots, 21-slot outage, stale latest session, one reconnect/degraded session, low-liquidity ADA 0-print windows, storage forecast breach preserving original DB, missing database, systemd multi-stage order and no order privilege. A synthetic 24-hour dataset is NOT a claim of actual server uptime.

The independent operational DB remains:
  /home/hong/apps/FreeQuantLab/state/microstructure/websocket.sqlite3

Command after deployment:

    cd /home/hong/apps/FreeQuantLab-observer-runtime
    /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.websocket_quality --enforce
    /home/hong/apps/FreeQuantLab/.venv/bin/python -m quantlab.websocket_microstructure --audit --audit-hours 48
    systemctl --user status freequant-websocket-audit.timer
    journalctl --user -u freequant-websocket-audit.service -n 25 --no-pager

No automatic retention/removal has been introduced. Any archiving/partitioning or longer continuous observation requires an explicit separate validation of recording costs and storage budgets.

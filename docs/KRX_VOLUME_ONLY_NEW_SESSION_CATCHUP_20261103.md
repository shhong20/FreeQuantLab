# 2026-11-03 KRX long-history gap recovery: source volume revisions

## Observed operational failure
On 2026-11-03 09:19 KST the registered freequant-stock-history.service performed a real append-only public market history refresh. It added Nov 2 US bars (AAPL/MSFT/SPY), but the long-history KRX book remained dated Oct 30 even though Nov 2 was observed in the independently SHA-backed daily observations store.

The captured Naver KRX raw source matched Samsung Electronics 005930 and SK hynix 000660 on open/high/low/close, but had revisions to volumes against the first observed daily snapshot. Existing capture_history() recorded the LIVE_BASELINE_VOLUME_REVISION then immediately continued, inadvertently skipping insertion into long_bars even when that vendor day did NOT yet exist.

KRX 069500 exhibits material OHLC disagreements with the independently retained early observations (plus historical provider revisions): it must remain unqualified without independent reconciliation.

## Safe change
- Preserve all original daily_bars, source snapshots, raw_history, first_raw_sha256, conflicts and frozen paper/QLib ledger evidence.
- New vendor-history day: when the only mismatch with the initial stock observation is VOLUME, append the independently SHA-backed public-vendor OHLCV day to the long history if no long_bars row exists. Retain an immutable LIVE_BASELINE_VOLUME_REVISION conflict and do NOT certify volume-based features.
- Existing vendor-history day: never update its immutable data, and do not double-count its long-vendor conflict when the baseline has a volume-only revision. The original difference remains recorded.
- MATERIAL open/high/low/close disagreement: continue to quarantine rather than insert new normalized long-history values for that day.
- Never synthesize candles, orders, virtual fills, strategy equity, or missed prospective daily observations.
- Overall history audits still report vendor-revision evidence and never qualify official corporate actions, stock total-return training, or live orders.
- The existing user-systemd weekly capture service keeps its failure exit code for unresolved genuine material discrepancies (e.g. 069500); a partial append is not a whole-pipeline success.

## Regression
Tests cover initial independent market-data observations, a new KRX long-history day with volume-only source difference, immutable raw SHA chain, preserved original observed volume, idempotent second refresh, no training/forward approval, and a genuinely changed OHLC value remaining absent from long history with an explicit conflict.

## Operational rollout
1. Run local full suite and PR CI.
2. Update **only** the stock observer runtime worktree used by existing freequant-stock-history.service, never the separate operating 90-day evaluation branch.
3. Trigger the already registered user-systemd stock history refresh once. Treat material provider-price conflicts as a legitimate nonzero exit; verify exact persisted date rows and source/conflict evidence independently.
4. Confirm the untouched clean 90-day virtual cohort still reports omissions rather than fabricating expired paper decisions.

This is public market history restoration only, not a new buy/sell recommendation.

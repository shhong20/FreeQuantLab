# Forward-only Shadow Paper incubation (2026-10-20)

**This system does not trade, does not authorize or promote any strategy, and does not backdate fills.** Its purpose is collecting *future* spot-market observations under frozen, explicit trading rules.

## Deployment on WSL

- Host `DESKTOP-S51Q473`, user `hong`, project `/home/hong/apps/FreeQuantLab`
- Database `state/shadow.db` is separate from the pre-existing `paper.db`. SQLite WAL, unique event key `(track_id,decision_date)`, one WAL account per asset×strategy.
- Read-only Upbit public daily candle and ticker API. For now only `KRW-BTC` and `KRW-ETH` are supported. No broker order credentials are needed or accepted.
- 3 immutable observation tracks each: 20%-exposed buy/hold, MA20/80 trend, and fixed historical-feature rule `r5 > 0`. The latter uses the same type of allowed feature as the research Agent but **is not the dynamically generated Exaone strategy nor a Qlib frozen model**.
- Each asset×strategy has a separate **1,000,000 virtual KRW**, 20% cap, 10 bps nominal fee + 5 bps slippage approximation; daily virtual loss 3% sets a persistent halt. Funds are NOT pooled and their returns must not be summed as portfolio P&L.
- Registered tracks start with zero virtual positions. A track can ONLY record a decision from a candle whose close date is *after* the registration candle, with a real observed quote from the **next UTC session**. It never retroactively invents fills. Today's still-open Upbit candle is not included.

### One-time registration

```bash
cd /home/hong/apps/FreeQuantLab
.venv/bin/python -m quantlab --config examples/shadow_crypto.json \
  --db state/shadow.db shadow-bootstrap
```

Running the same command again is idempotent. Attempting to replace a strategy or settings snapshot with another rule under the same name is rejected.

### Safe scheduler

Templates under `deploy/systemd/` are intended for the WSL `hong` user and use the dedicated Python virtualenv. The WSL host is configured as `Asia/Seoul`.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/freequant-shadow.{service,timer} ~/.config/systemd/user/
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user daemon-reload
systemctl --user enable --now freequant-shadow.timer
systemctl --user list-timers --all freequant-shadow.timer
```

Timer checks at 09:15, 09:25, 09:35, and 10:00 KST (daily UTC day close is 09:00 KST). If a daily attempt fails the next check can retry; if a previous attempt already recorded this session it skips without placing another order. `Persistent=true` allows a catch-up check when WSL starts again, but it does not replay missed days at fictional historical quotes. **WSL/Windows must actually be running** or user units cannot execute.

To manually exercise status without creating a new trade:

```bash
.venv/bin/python -m quantlab --db state/shadow.db shadow-status
```

To run an immediate check (will not trade until the next newly completed candle):

```bash
.venv/bin/python -m quantlab --config examples/shadow_crypto.json \
  --db state/shadow.db shadow-cycle
```

### Monitoring and failures

```bash
systemctl --user status freequant-shadow.timer
journalctl --user -u freequant-shadow.service -n 80 --no-pager
.venv/bin/python -m quantlab --db state/shadow.db shadow-status
```

- `shadow_runs`: persisted per-invocation status and JSON summary
- `shadow_events`: immutable per-track per-date quote/fill/hold/stop event
- `shadow_accounts`: per-track frozen config, cash, unit balance, last-marked equity, halt state
- `state/shadow.db.lock`: process-level flock prevents simultaneous cycles, SQLite key prevents duplicate records
- If stale daily candles, stale ticker (over 300 seconds), a missed session, data/API error, or a held account is found, the system fails closed, logs the problem, and never backfills at an old candle's price.
- Runtime database and logs are ignored by Git and are not pushed to GitHub.
- The first scheduled actual shadow execution is **only eligible following a newly closed UTC daily candle after registration**, even if registration and first systemd check occur on the same day.

### Limitations

Paper fills are approximations using the latest public trade quote, not tradable bid/ask or actual exchange matching. Market impact, spread, taxes, outages and the actual account balance may differ. The current module is **isolated from** KIS and Naver history (no domestic stock paper feed without authenticated real-time quotes and exchange-calendar checks), Qlib model promotion and all live broker order execution. Eligibility for live deployment remains **always false**, regardless of observed returns. A separate explicit authorization and broker-specific order/reconciliation design is required for any eventual trading use.

# Reliable local Watchdog alerts when Windows Toast is unavailable

**Observed 2026-10-21 KST, WSL2 host DESKTOP-S51Q473**

After the pre-window exposure forensic warning was added, an actual manual `freequant-watchdog.service` run returned exit code **2**. Investigation showed this was *not* a failure of the read-only paper ledger audit or reporting: `state/watchdog-history.json` contained the durable `PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY` event, and the independent report `reports/forward/2026-10-21.json` had been produced. The issue was `windows_delivery.delivery_failed=1` because the WSL→Windows desktop toast sender could not reach its notification API.

The original service had specified `--windows-toast` for every scheduled check, causing one failed presentation attempt to mark an otherwise complete **local** audit as a failed systemd job. In addition, the isolated Exaone/Qlib watchdog had the same risk.

## Adjusted service policy

Both scheduled service definitions, `deploy/systemd/freequant-watchdog.service` and `deploy/systemd/freequant-isolated-watchdog.service`, now omit the optional `--windows-toast` argument. Every scheduled run still persists alert transitions, duplicates and resolutions in its own SQLite DB, exports a local JSON inbox and daily report, and exposes those reports over the existing localhost dashboard read-only API. The service fails if *the actual watchdog audit or local durable alert persistence* fails; it no longer depends on the Windows desktop notification subsystem to run correctly.

The `--windows-toast` CLI capability has **not been deleted**, and may be manually enabled later after an explicit Windows notification setup and real test. By default there is **no Windows pop-up notification**, so operators must review the dashboard, JSON alert history or systemd journal. A green systemd service proves local audit completion only, **not a notification on the user's screen**.

This change is independent of `state/market_history.db` Upbit real OHLCV recovery, the three original virtual paper ledgers, historical positions, strategy/model checkpoints and the locked 90-day trial.

## Read-only verification

```bash
cd /home/hong/apps/FreeQuantLab
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus

systemctl --user status freequant-watchdog.service freequant-isolated-watchdog.service --no-pager
curl -fsS http://127.0.0.1:8788/api/alerts
curl -fsS http://127.0.0.1:8788/api/isolated-alerts
journalctl --user -u freequant-watchdog.service -u freequant-isolated-watchdog.service -n 60 --no-pager
```

If the WSL distribution or Windows desktop is off, systemd cannot independently wake it; on restart the archive timer catches up **real historical market candles**, not unobserved simulated orders. Missing paper dates remain missing in the ledger and are warned about.

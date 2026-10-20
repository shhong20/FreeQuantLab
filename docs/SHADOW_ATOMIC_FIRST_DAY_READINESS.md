# Atomic Shadow Paper batches · first live-candle verification

2026-10-20 KST — WSL2 host DESKTOP-S51Q473. Research-only, no broker orders.

## Critical issue fixed

Previously `quantlab.shadow.run_cycle` committed one public Upbit quote and then each of the three baseline virtual strategy trades in **different transactions**. If a later strategy failed, earlier account events or quote evidence could persist, and the immutable-quote conflict would prevent next timed retries from recovering the UTC day.

Now **one asset / one UTC decision day = one SQLite transaction**. Frozen-trade decision computation, quote evidence, all corresponding account balance/units/virtual events are committed as a batch, or **all rolled back**. A BTC failure does not affect a complete ETH batch. Only on successful commit do the generated virtual results appear in the success list. A cycle ERROR is still logged for independent watchdog inspection.

Two SQL fault-injection regressions deliberately force an error on the *second* of three BTC strategies and on the first strategy. They verify zero BTC events/evidence and unchanged BTC cash after failure, independent committed ETH success, and successful BTC-only retry with no duplicate ETH quote/trades after removing the injected trigger. Forensic virtual-ledger and quote-evidence audits pass before/after retry.

Already-present partial historical fills are **not** auto-repaired. No strategy thresholds, frozen registration identity, model artifacts, fee/slippage, real-order settings or dates are changed.

## Host state verified before first bar

- WSL timezone Asia/Seoul, NTP synced, user manager `Linger=yes`.
- Five `freequant-*.timer` units active, no failed user units.
- Original 6, Exaone 2 and Qlib 2 virtual accounts all 0 genuine new observations; all audited PASS.
- Supplemental Exaone/Qlib first eligible UTC day is Oct 20; expected first observation after **Oct 21 09:00 KST** close. Original six-account 90-day scoring starts UTC Oct 21, so first scoring observation is after **Oct 22 09:00 KST**.
- Grace period for missed-day warnings: **10:20 KST** next day.
- WSL linger does not prevent Windows sleeping or distribution shutdown. If offline, report missing dates; **never fabricate backdated fills**.

## Read-only first-day check

```bash
cd /home/hong/apps/FreeQuantLab
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user list-timers --all 'freequant*'
systemctl --user --failed
.venv/bin/python -m quantlab --db state/shadow.db protocol-check \
  --protocol protocols/2026-10-20-btc-eth-90d.json
.venv/bin/python -m quantlab.qlib_lab status \
  --plan protocols/2026-10-20-qlib-lab.json --db state/qlib_lab_shadow.db
.venv/bin/python -m quantlab.lane_comparison
curl -fsS http://127.0.0.1:8788/api/isolated-status
journalctl --user -u freequant-shadow.service -u freequant-exaone.service \
  -u freequant-qlib-lab.service -n 80 --no-pager
```

Simulated public-ticker virtual fills are not real executable exchange orders or evidence of investment profitability.

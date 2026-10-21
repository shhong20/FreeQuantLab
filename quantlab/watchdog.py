"""Persistent deduplicated operational alerts and daily forward-only reports.

All alerts stay in an independent local outbox even if a desktop notification
cannot be delivered. No private credentials or network dependencies.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Callable
from urllib.request import urlopen

from .forward_reports import report as build_report, save_report, _atomic
from .monitor import snapshot
from .ledger_audit import audit_shadow_ledger, evaluate_manual_review
from .quote_quality import execution_proxy_quality

WINDOWS_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
# Only activated for real anomaly transitions, never during an initial no-alert check.
_TOAST_PS = r'''
$ErrorActionPreference = "Stop"
$encoded = $args[0]
$data = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded)) | ConvertFrom-Json
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
[Windows.UI.Notifications.ToastTemplateType,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $xml.GetElementsByTagName("text")
$text.Item(0).AppendChild($xml.CreateTextNode([string]$data.title)) > $null
$text.Item(1).AppendChild($xml.CreateTextNode([string]$data.body)) > $null
$notification = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Microsoft.Windows.PowerShell").Show($notification)
Write-Output "Toast submitted to Windows notification API"
'''


def windows_toast(title: str, body: str) -> None:
    """Windows best effort. 0 exit indicates accepted API invocation, not UI visibility."""
    if not Path(WINDOWS_POWERSHELL).is_file():
        raise RuntimeError("Windows PowerShell executable unavailable")
    payload = base64.b64encode(json.dumps({
        "title": title[:90], "body": body[:230],
    }, ensure_ascii=False).encode("utf-8")).decode()
    # Encode the script itself to avoid shell interpolation and quoting injection.
    cmd = _TOAST_PS.replace("$args[0]", "'" + payload + "'")
    script = base64.b64encode(cmd.encode("utf-16le")).decode()
    result = subprocess.run(
        [WINDOWS_POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
         "-EncodedCommand", script],
        capture_output=True, text=True, timeout=18, check=False,
    )
    if result.returncode != 0 or "Toast submitted" not in result.stdout:
        raise RuntimeError("Windows toast API rejected the notification")


def _open_state(path: str | Path) -> sqlite3.Connection:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(out, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_alerts (
          alert_key TEXT PRIMARY KEY,
          severity TEXT NOT NULL,
          code TEXT NOT NULL,
          message TEXT NOT NULL,
          first_seen TEXT NOT NULL,
          last_seen TEXT NOT NULL,
          last_notified TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_time TEXT NOT NULL,
          alert_key TEXT NOT NULL,
          event_kind TEXT NOT NULL,
          severity TEXT NOT NULL,
          code TEXT NOT NULL,
          message TEXT NOT NULL,
          delivery_status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT
        )
    """)
    return conn


def _key(item: dict) -> str:
    # Keep separate daily events for daily-missing-bar alerts.
    return "|".join(str(item.get(k, "")) for k in (
        "code", "symbol", "strategy", "expected_decision_date"
    ))


def _synthetic_alert(code: str, message: str) -> dict:
    return {"severity": "critical", "code": code, "message": message}


def persist_alert_transitions(
    db: str | Path, alerts: list[dict], *, now: datetime,
    remind_after: timedelta = timedelta(hours=24),
) -> dict:
    if now.tzinfo is None:
        raise ValueError("Aware timestamp required")
    timestamp = now.astimezone(timezone.utc).isoformat()
    current = {_key(a): a for a in alerts}
    created = []
    with _open_state(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = {
            r[0]: {"severity": r[1], "code": r[2], "message": r[3],
                   "last_notified": r[4]}
            for r in conn.execute(
                "SELECT alert_key,severity,code,message,last_notified FROM active_alerts"
            )
        }
        for key, item in current.items():
            previous = old.get(key)
            kind = None
            if previous is None:
                kind = "triggered"
                conn.execute(
                    "INSERT INTO active_alerts VALUES (?,?,?,?,?,?,?)",
                    (key,item["severity"],item["code"],item["message"],
                     timestamp,timestamp,timestamp),
                )
            else:
                from datetime import datetime as _datetime
                last = _datetime.fromisoformat(previous["last_notified"])
                if previous["severity"] != item["severity"]:
                    kind = "severity_changed"
                elif item["severity"] == "critical" and now - last >= remind_after:
                    kind = "critical_reminder"
                conn.execute(
                    "UPDATE active_alerts SET severity=?,code=?,message=?,last_seen=?,"
                    "last_notified=? WHERE alert_key=?",
                    (item["severity"],item["code"],item["message"],timestamp,
                     timestamp if kind else previous["last_notified"],key),
                )
            if kind:
                conn.execute(
                    "INSERT INTO notification_events "
                    "(event_time,alert_key,event_kind,severity,code,message) "
                    "VALUES(?,?,?,?,?,?)",
                    (timestamp,key,kind,item["severity"],item["code"],
                     str(item["message"])[:300]),
                )
                created.append({"key":key,"kind":kind,"code":item["code"]})
        for key, previous in old.items():
            if key not in current:
                conn.execute("DELETE FROM active_alerts WHERE alert_key=?", (key,))
                conn.execute(
                    "INSERT INTO notification_events "
                    "(event_time,alert_key,event_kind,severity,code,message) "
                    "VALUES(?,?,?,?,?,?)",
                    (timestamp,key,"resolved","info",previous["code"],
                     "Previously active issue is no longer detected."),
                )
                created.append({"key":key,"kind":"resolved","code":previous["code"]})
    return {"active_alerts":len(current),"transitions":created}


def deliver_pending(
    state_db: str | Path, *, toast: bool, notify: Callable[[str,str],None] = windows_toast,
    now: datetime | None = None,
) -> dict:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Aware timestamp required")
    sent, failed, disabled = 0,0,0
    with _open_state(state_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            "SELECT id,event_time,event_kind,severity,code,message "
            "FROM notification_events WHERE delivery_status IN ('pending','failed') "
            "AND attempts < 3 ORDER BY id LIMIT 30"
        ).fetchall()
        for eid,created,kind,severity,code,msg in pending:
            # Don't replay stale toast notifications when WSL comes online days later.
            stale = instant - datetime.fromisoformat(created) > timedelta(days=1)
            if not toast or stale:
                state = "not_requested" if not toast else "stale_skipped"
                conn.execute(
                    "UPDATE notification_events SET delivery_status=? WHERE id=?",
                    (state,eid),
                )
                disabled += 1
                continue
            title = "FreeQuantLab · " + ("복구" if kind == "resolved" else
                                         "중대 경고" if severity == "critical" else "운영 경고")
            try:
                notify(title, f"{code} · {msg}"[:220])
            except Exception as exc:
                # Errors in notification delivery NEVER affect the paper ledger.
                failed += 1
                conn.execute(
                    "UPDATE notification_events SET attempts=attempts+1,"
                    "delivery_status='failed',last_error=? WHERE id=?",
                    (type(exc).__name__[:80],eid),
                )
            else:
                sent += 1
                conn.execute(
                    "UPDATE notification_events SET attempts=attempts+1,"
                    "delivery_status='submitted',last_error=NULL WHERE id=?",
                    (eid,),
                )
    return {"submitted_to_windows_api":sent,"delivery_failed":failed,
            "local_only":disabled,
            "delivery_note":"Submitted does not prove a desktop notification was displayed"}


def export_alert_history(
    state_db: str | Path, target_file: str | Path,
    *, now: datetime | None = None,
) -> Path:
    """Publish one atomic JSON inbox snapshot; dashboard never opens WAL db."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Aware timestamp required")
    with _open_state(state_db) as con:
        rows = con.execute(
            "SELECT event_time,event_kind,severity,code,message,delivery_status "
            "FROM notification_events ORDER BY id DESC LIMIT 100"
        ).fetchall()
    payload = {
        "generated_at_utc":instant.astimezone(timezone.utc).isoformat(),
        "events":[{
            "time":r[0],"kind":r[1],"severity":r[2],
            "code":r[3],"message":r[4],"delivery":r[5],
        } for r in rows],
        "read_only":True,
    }
    target = Path(target_file)
    _atomic(target,(json.dumps(payload,ensure_ascii=False,allow_nan=False,indent=2)+"\n").encode("utf-8"))
    return target


def check(
    *, shadow_db: str | Path, state_db: str | Path,
    report_dir: str | Path, now: datetime | None = None,
    windows_notifications: bool = False,
    health_check: Callable[[],bool] | None = None,
    protocol_path: str | Path | None = None,
) -> dict:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Aware timestamp required")
    alerts = []
    daily_paths = None
    state = "OK"
    compliance = None
    if protocol_path is not None:
        try:
            from .protocol_compliance import assess_collection
            compliance = assess_collection(shadow_db, protocol_path, now=instant)
            if compliance["status"] in ("INVALID_LEDGER", "INVALID_PROTOCOL_IDENTITY"):
                state = "DEGRADED"
                alerts.append(_synthetic_alert(
                    "PROTOCOL_IDENTITY_OR_LEDGER_FAILED",
                    "Forward protocol account identity or ledger verification failed."))
            elif compliance["status"] == "BREACH_OR_MISSING":
                observed_issues={x["code"] for x in compliance.get("issues",[])}
                if "PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY" in observed_issues:
                    alerts.append({
                        "severity":"warning",
                        "code":"PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY",
                        "message":"Original 90-day virtual ledger contains real-time "
                                  "simulated positions recorded before its locked "
                                  "evaluation start. Preserve and review opening "
                                  "inventory; never silently count as 90-day alpha.",
                    })
                if observed_issues-{"PROTOCOL_PRE_WINDOW_VIRTUAL_ACTIVITY"}:
                    alerts.append({
                        "severity": "warning",
                        "code": "PROTOCOL_DUE_OBSERVATIONS_MISSING",
                        "message": "Due forward UTC observations missing or inconsistent. No backfill is allowed.",
                    })
        except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError) as exc:
            state = "DEGRADED"
            compliance = {"status":"UNAVAILABLE", "real_orders_enabled":False}
            alerts.append(_synthetic_alert(
                "PROTOCOL_CHECK_FAILED",
                "Unable to verify the locked forward research protocol."))
    try:
        snap = snapshot(shadow_db, now=instant)
        alerts.extend(snap["alerts"])
        if snap["recent_runs"] and snap["recent_runs"][0]["status"]=="ERROR":
            last_errors = snap["recent_runs"][0].get("errors", [])
            if any(
                "MarketIntegrityError" in str(e.get("error","")) or
                "DataError" in str(e.get("error",""))
                for e in last_errors if isinstance(e,dict)
            ):
                alerts.append(_synthetic_alert(
                    "MARKET_DATA_QUALITY_REJECTED",
                    "Latest forward paper cycle rejected market feed or public quote.",
                ))
        ledger_evidence = audit_shadow_ledger(shadow_db, now=instant)
        if ledger_evidence["status"] != "PASS":
            state = "DEGRADED"
            alerts.append(_synthetic_alert(
                "SHADOW_LEDGER_INTEGRITY_FAILED",
                "Read-only replay found mismatched virtual accounting. Manual investigation required.",
            ))
            # No nominal daily performance report on compromised accounting.
        elif state != "DEGRADED":
            quality = execution_proxy_quality(shadow_db)
            if quality["status"] == "FAIL":
                state = "DEGRADED"
                alerts.append(_synthetic_alert(
                    "PAPER_QUOTE_EVIDENCE_MISMATCH",
                    "A saved public ticker / simulated virtual fill inconsistency was detected.",
                ))
            else:
                report_data = build_report(shadow_db, now=instant)
                report_data["execution_proxy_quality"] = quality
                report_data["ledger_integrity"] = {
                    "status": "PASS", "accounts": ledger_evidence["accounts"],
                    "events": ledger_evidence["events"],
                }
                report_data["manual_review"] = evaluate_manual_review(
                    report_data, ledger_evidence
                )
                if compliance is not None:
                    report_data["protocol_collection_compliance"] = compliance
                daily_paths = save_report(report_data, report_dir)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        state = "DEGRADED"
        alerts.append(_synthetic_alert(
            "SHADOW_READ_FAILED",
            "Unable to read shadow paper ledger; no report was fabricated.",
        ))
    if health_check is not None:
        try:
            healthy = bool(health_check())
        except Exception:
            healthy = False
        if not healthy:
            alerts.append({
                "severity":"warning","code":"DASHBOARD_UNREACHABLE",
                "message":"Local monitoring dashboard health check failed.",
            })
    transitions = persist_alert_transitions(state_db, alerts, now=instant)
    delivery = deliver_pending(state_db, toast=windows_notifications, now=instant)
    inbox_file = export_alert_history(state_db, Path(state_db).with_name(Path(state_db).stem + "-history.json"), now=instant)
    return {
        "status":state,
        "checked_at_utc":instant.astimezone(timezone.utc).isoformat(),
        "active_alerts":transitions["active_alerts"],
        "alert_transitions":transitions["transitions"],
        "windows_delivery":delivery,
        "reports":[str(p) for p in daily_paths] if daily_paths else [],
        "inbox_snapshot":str(inbox_file),
        "protocol_compliance":compliance,
        "real_orders_enabled":False,
    }


def dashboard_health() -> bool:
    with urlopen("http://127.0.0.1:8788/healthz",timeout=5) as response:
        return response.status==200 and json.load(response).get("status")=="ok"


def main(argv=None):
    parser=argparse.ArgumentParser(description="FreeQuantLab read-only daily watchdog")
    parser.add_argument("--shadow-db",default="state/shadow.db")
    parser.add_argument("--state-db",default="state/watchdog.db")
    parser.add_argument("--report-dir",default="reports/forward")
    parser.add_argument("--windows-toast",action="store_true")
    parser.add_argument("--skip-dashboard-health",action="store_true")
    parser.add_argument("--protocol",help="Frozen prospective manifest for read-only due-date checks")
    args=parser.parse_args(argv)
    try:
        result=check(
            shadow_db=args.shadow_db,state_db=args.state_db,
            report_dir=args.report_dir,windows_notifications=args.windows_toast,
            health_check=None if args.skip_dashboard_health else dashboard_health,
            protocol_path=args.protocol,
        )
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result["status"]=="OK" and result["windows_delivery"]["delivery_failed"]==0 else 2
    except (OSError,sqlite3.Error,ValueError,RuntimeError) as exc:
        print(f"WATCHDOG FAILED CLOSED: {type(exc).__name__}: {exc}",file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

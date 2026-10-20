"""Loopback-only, read-only Shadow Paper dashboard server.

No dependencies beyond Python stdlib and the installed FreeQuantLab package.
No external CDNs, credentials, mutation routes or real broker endpoints.
"""
from __future__ import annotations

import argparse
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from .monitor import snapshot

STATIC = Path(__file__).parent / "static"
ROUTES = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def make_handler(db_path: str | Path, *, reports_dir: str | Path = "reports/forward",
                 alerts_db: str | Path = "state/watchdog.db"):
    """Dedicated handler factory used by tests and isolated service."""
    database = Path(db_path).resolve()
    daily_dir = Path(reports_dir).resolve()
    alert_store = Path(alerts_db).resolve()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "FreeQuantLabMonitor/0.1"
        sys_version = ""

        def log_message(self, fmt, *args):
            # No payload or header dumping; only local HTTP request metadata.
            sys.stderr.write("dashboard %s %s\n" % (self.log_date_time_string(), fmt % args))

        def _headers(self, status: int, mime: str, size: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self';"
                " connect-src 'self'; img-src 'self'; base-uri 'none';"
                " frame-ancestors 'none'; form-action 'none'"
            )
            self.end_headers()

        def _reply(self, status: int, obj: dict | str | bytes, mime: str) -> None:
            payload = (json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
                       if isinstance(obj, dict) else
                       obj.encode("utf-8") if isinstance(obj, str) else obj)
            self._headers(status, mime, len(payload))
            self.wfile.write(payload)

        def _valid_host(self) -> bool:
            port = self.server.server_address[1]
            supplied = self.headers.get("Host", "").lower()
            return supplied in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self):
            if not self._valid_host():
                self._reply(403, {"error": "Only localhost Host headers are allowed"},
                            "application/json; charset=utf-8")
                return
            path = urlsplit(self.path).path
            if path == "/api/status":
                try:
                    self._reply(200, snapshot(database), "application/json; charset=utf-8")
                except (OSError, ValueError, KeyError, RuntimeError):
                    # Avoid exposing DB path, SQL, internals or API credentials.
                    self._reply(503, {"status": "unavailable",
                                      "error": "Read-only shadow ledger is unavailable"},
                                "application/json; charset=utf-8")
                return
            if path == "/api/latest-report":
                try:
                    found = sorted(daily_dir.glob("????-??-??.json"))
                    if not found:
                        self._reply(404, {"status": "not_ready"}, "application/json; charset=utf-8")
                    else:
                        data = json.loads(found[-1].read_text(encoding="utf-8"))
                        if not isinstance(data, dict):
                            raise ValueError("Invalid daily report")
                        self._reply(200, data, "application/json; charset=utf-8")
                except (OSError, ValueError):
                    self._reply(503, {"status": "unavailable"}, "application/json; charset=utf-8")
                return
            if path == "/api/alert-history":
                try:
                    if not alert_store.is_file():
                        self._reply(200, {"events": []}, "application/json; charset=utf-8")
                        return
                    with sqlite3.connect(alert_store.as_uri()+"?mode=ro", uri=True, timeout=3) as con:
                        con.execute("PRAGMA query_only=ON")
                        rows = con.execute(
                            "SELECT event_time,event_kind,severity,code,message,delivery_status "
                            "FROM notification_events ORDER BY id DESC LIMIT 30"
                        ).fetchall()
                    items = [
                        {"time":r[0],"kind":r[1],"severity":r[2],
                         "code":r[3],"message":r[4],"delivery":r[5]}
                        for r in rows
                    ]
                    self._reply(200, {"events": items}, "application/json; charset=utf-8")
                except (OSError, sqlite3.Error, ValueError):
                    self._reply(503, {"status": "unavailable"}, "application/json; charset=utf-8")
                return
            if path == "/healthz":
                try:
                    summary = snapshot(database)["summary"]
                    self._reply(200, {
                        "status": "ok", "read_only": True,
                        "registered_tracks": summary["tracks"]
                    }, "application/json; charset=utf-8")
                except (OSError, ValueError, KeyError, RuntimeError):
                    self._reply(503, {"status": "unavailable"},
                                "application/json; charset=utf-8")
                return
            if path in ROUTES:
                file, mime = ROUTES[path]
                try:
                    body = (STATIC / file).read_bytes()
                except OSError:
                    self._reply(503, {"status": "unavailable"}, "application/json; charset=utf-8")
                    return
                self._reply(200, body, mime)
                return
            self._reply(404, {"status": "not_found"}, "application/json; charset=utf-8")

        def do_POST(self):
            self._reply(405, {"error": "Read-only dashboard: writes are forbidden"},
                        "application/json; charset=utf-8")

        def do_PUT(self):
            self.do_POST()

        def do_DELETE(self):
            self.do_POST()

    return DashboardHandler


def serve(*, db: str | Path, bind: str = "127.0.0.1", port: int = 8788):
    try:
        addr = ipaddress.ip_address(bind)
    except ValueError as exc:
        raise ValueError("Bind must be a numeric loopback IP, not a hostname") from exc
    if not addr.is_loopback:
        raise ValueError("Dashboard can only bind to local loopback")
    if not 1024 <= port <= 65535:
        raise ValueError("Unsupported dashboard port")
    server = ThreadingHTTPServer((bind, port), make_handler(db))
    print(f"FreeQuantLab read-only dashboard: http://{bind}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local-only read-only FreeQuantLab monitoring")
    parser.add_argument("--db", default="state/shadow.db")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args(argv)
    try:
        serve(db=args.db, bind=args.bind, port=args.port)
    except (OSError, ValueError) as exc:
        print(f"Dashboard startup blocked: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

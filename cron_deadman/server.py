"""HTTP endpoint that receives pings, plus the background watcher that sends alerts."""

from __future__ import annotations

import hmac
import json
import logging
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import DOWN, FAILED, UP, Store, describe_transition

log = logging.getLogger("cron_deadman")


class Notifier:
    """Logs every transition and, if configured, posts it to a Slack-compatible webhook."""

    def __init__(self, webhook: str | None = None, timeout: float = 10):
        self.webhook = webhook
        self.timeout = timeout

    def __call__(self, message: str) -> None:
        log.warning(message)
        if not self.webhook:
            return
        req = urllib.request.Request(
            self.webhook,
            data=json.dumps({"text": f"cron-deadman: {message}"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=self.timeout).close()  # noqa: S310 - operator-provided URL
        except OSError as exc:
            log.error("webhook delivery failed: %s", exc)


def notify_transitions(store: Store, transitions, notify) -> None:
    for check, old, new in transitions:
        msg = describe_transition(check, old, new, store.now())
        if msg:
            notify(msg)


def metrics(store: Store) -> str:
    lines = [
        "# HELP deadman_check_up 1 if the check pinged successfully within period+grace.",
        "# TYPE deadman_check_up gauge",
    ]
    snap = store.snapshot()
    lines += [f'deadman_check_up{{check="{c["name"]}"}} {int(c["status"] == UP)}' for c in snap]
    lines += [
        "# HELP deadman_last_ping_timestamp_seconds Unix time of the last finished run.",
        "# TYPE deadman_last_ping_timestamp_seconds gauge",
    ]
    lines += [f'deadman_last_ping_timestamp_seconds{{check="{c["name"]}"}} {c["last_ping"] or 0}' for c in snap]
    lines += [
        "# HELP deadman_last_duration_seconds Duration of the last run (needs a /start ping).",
        "# TYPE deadman_last_duration_seconds gauge",
    ]
    lines += [
        f'deadman_last_duration_seconds{{check="{c["name"]}"}} {c["last_duration"]}'
        for c in snap
        if c["last_duration"] is not None
    ]
    return "\n".join(lines) + "\n"


def make_handler(store: Store, notify, token: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cron-deadman"

        def _send(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, query: dict) -> bool:
            if not token:
                return True
            given = self.headers.get("Authorization", "").removeprefix("Bearer ") or query.get("token", [""])[0]
            return hmac.compare_digest(given, token)

        def _handle(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            parts = [p for p in url.path.split("/") if p]
            if parts == ["healthz"]:
                return self._send(200, "ok\n")
            if parts == ["status"]:
                return self._send(200, json.dumps(store.snapshot(), indent=2) + "\n", "application/json")
            if parts == ["metrics"]:
                return self._send(200, metrics(store), "text/plain; version=0.0.4")
            if len(parts) in (2, 3) and parts[0] == "ping":
                if not self._authorized(query):
                    return self._send(401, "unauthorized\n")
                kind = parts[2] if len(parts) == 3 else "success"
                if kind not in ("start", "fail", "success"):
                    return self._send(404, "unknown ping type\n")
                try:
                    code = int(query["code"][0]) if "code" in query else None
                    transitions = store.ping(parts[1], kind, code)
                except KeyError:
                    return self._send(404, f"unknown check '{parts[1]}'\n")
                except ValueError:
                    return self._send(400, "code must be an integer\n")
                notify_transitions(store, transitions, notify)
                return self._send(200, "ok\n")
            return self._send(404, "not found\n")

        do_GET = do_POST = do_HEAD = _handle

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


class Watcher(threading.Thread):
    def __init__(self, store: Store, notify, interval: float = 30):
        super().__init__(daemon=True)
        self.store, self.notify, self.interval = store, notify, interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            notify_transitions(self.store, self.store.evaluate(), self.notify)


def serve(store: Store, notify, host: str, port: int, interval: float, token: str | None = None) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(store, notify, token))
    watcher = Watcher(store, notify, interval)
    watcher.start()
    notify_transitions(store, store.evaluate(), notify)
    down = [c.name for c in store.checks.values() if c.status in (DOWN, FAILED)]
    log.info("listening on http://%s:%d with %d check(s)", host, port, len(store.checks))
    if down:
        log.warning("currently failing: %s", ", ".join(down))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop_event.set()
        httpd.server_close()

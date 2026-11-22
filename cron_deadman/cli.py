"""Command-line entry point: `serve` the heartbeat endpoint or `run` a job and report its result."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import urllib.request

from . import __version__
from .core import Store, load_config
from .server import Notifier, serve


def ping(url: str, token: str | None = None, timeout: float = 10) -> bool:
    """Best-effort ping: a monitoring outage must never break the job itself."""
    req = urllib.request.Request(url, data=b"", method="POST")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(req, timeout=timeout).close()  # noqa: S310 - operator-provided URL
        return True
    except OSError as exc:
        print(f"cron-deadman: could not ping {url}: {exc}", file=sys.stderr)
        return False


def run_job(url: str, command: list[str], token: str | None = None) -> int:
    url = url.rstrip("/")
    ping(f"{url}/start", token)
    try:
        code = subprocess.call(command)
    except OSError as exc:  # command not found, not executable
        print(f"cron-deadman: cannot run {command[0]!r}: {exc}", file=sys.stderr)
        code = 127
    ping(url if code == 0 else f"{url}/fail?code={code}", token)
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cron-deadman",
        description="Dead man's switch for cron jobs: alert when a job fails or silently stops running.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the heartbeat server")
    s.add_argument("-c", "--config", required=True, help="YAML file with checks (and optional webhook)")
    s.add_argument("--state", default="deadman-state.json", help="state file (default: deadman-state.json)")
    s.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--interval", type=float, default=30, help="seconds between overdue checks (default 30)")
    s.add_argument("--webhook", help="Slack-compatible webhook URL (overrides the config file)")
    s.add_argument("-v", "--verbose", action="store_true", help="log every request")

    r = sub.add_parser("run", help="run a command and report start/success/failure to the server")
    r.add_argument("url", help="ping URL of the check, e.g. http://monitor:8080/ping/nightly-backup")
    r.add_argument("command", nargs=argparse.REMAINDER, help="command to run (put it after --)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get("DEADMAN_TOKEN") or None
    if args.cmd == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            print("cron-deadman run: no command given", file=sys.stderr)
            return 2
        return run_job(args.url, command, token)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = load_config(args.config)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: bad config {args.config}: {exc}", file=sys.stderr)
        return 2
    store = Store(config, args.state)
    serve(store, Notifier(args.webhook or config["webhook"]), args.host, args.port, args.interval, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())

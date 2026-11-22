"""Check definitions, persistent state and the overdue/failed evaluation logic."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

NEW, UP, DOWN, FAILED = "new", "up", "down", "failed"
_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_duration(value: str | int | float) -> float:
    """'90s', '15m', '1h30m', '1d', '2w' or a plain number of seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip().lower().replace(" ", "")
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    parts = _DUR_RE.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"invalid duration: {value!r}")
    return sum(float(n) * _UNIT[u] for n, u in parts)


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    """Largest two units, e.g. '1d', '1h30m', '45s'."""
    rest = int(seconds)
    parts = []
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if rest >= size:
            parts.append(f"{rest // size}{unit}")
            rest %= size
    return "".join(parts[:2]) or "0s"


@dataclass
class Check:
    name: str
    period: float
    grace: float
    first_seen: float
    last_ping: float | None = None
    last_start: float | None = None
    last_duration: float | None = None
    last_exit_code: int | None = None
    status: str = NEW

    def deadline(self) -> float:
        return (self.last_ping or self.first_seen) + self.period + self.grace

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    checks = []
    for c in data.get("checks") or []:
        name = str(c["name"])
        if not _NAME_RE.match(name):
            raise ValueError(f"check name {name!r} may only contain letters, digits, '.', '_' and '-'")
        checks.append({"name": name, "period": parse_duration(c["period"]), "grace": parse_duration(c.get("grace", 0))})
    if len({c["name"] for c in checks}) != len(checks):
        raise ValueError("duplicate check names in config")
    return {"webhook": data.get("webhook"), "checks": checks}


class Store:
    """Thread-safe check registry persisted to a JSON file so restarts don't lose history."""

    def __init__(self, config: dict, state_path: str | Path | None = None, now=time.time):
        self.now = now
        self.lock = threading.Lock()
        self.state_path = Path(state_path) if state_path else None
        saved = {}
        if self.state_path and self.state_path.exists():
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        t = self.now()
        self.checks: dict[str, Check] = {}
        for c in config["checks"]:
            prev = saved.get(c["name"], {})
            prev.update(name=c["name"], period=c["period"], grace=c["grace"])  # config wins
            prev.setdefault("first_seen", t)
            self.checks[c["name"]] = Check(**prev)

    def save(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({n: c.to_dict() for n, c in self.checks.items()}, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)  # atomic on POSIX

    def ping(self, name: str, kind: str = "success", exit_code: int | None = None) -> list[tuple[Check, str, str]]:
        """Record a ping. kind is 'success', 'fail' or 'start'. Returns status transitions."""
        with self.lock:
            check = self.checks[name]  # KeyError for unknown checks
            t = self.now()
            if kind == "start":
                check.last_start = t
                self.save()
                return []
            check.last_ping = t
            check.last_duration = t - check.last_start if check.last_start else None
            check.last_start = None
            check.last_exit_code = exit_code if exit_code is not None else (0 if kind == "success" else 1)
            old, check.status = check.status, (UP if kind == "success" else FAILED)
            self.save()
            return [(check, old, check.status)] if old != check.status else []

    def evaluate(self) -> list[tuple[Check, str, str]]:
        """Mark checks DOWN once their deadline passes. Returns status transitions."""
        transitions = []
        with self.lock:
            t = self.now()
            for check in self.checks.values():
                if check.status in (UP, NEW) and t > check.deadline():
                    transitions.append((check, check.status, DOWN))
                    check.status = DOWN
            if transitions:
                self.save()
        return transitions

    def snapshot(self) -> list[dict]:
        with self.lock:
            t = self.now()
            out = []
            for c in self.checks.values():
                d = c.to_dict()
                d["seconds_until_deadline"] = round(c.deadline() - t, 1)
                out.append(d)
            return out


def describe_transition(check: Check, old: str, new: str, now: float) -> str:
    if new == DOWN:
        since = (
            "has never pinged" if check.last_ping is None else f"last ping {fmt_duration(now - check.last_ping)} ago"
        )
        return f"DOWN: '{check.name}' is overdue ({since}; expected every {fmt_duration(check.period)})"
    if new == FAILED:
        return f"FAILED: '{check.name}' reported failure (exit code {check.last_exit_code})"
    if new == UP and old in (DOWN, FAILED):
        return f"RECOVERED: '{check.name}' is back up"
    return ""

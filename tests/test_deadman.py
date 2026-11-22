import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from cron_deadman.cli import main, run_job
from cron_deadman.core import DOWN, FAILED, NEW, UP, Store, fmt_duration, load_config, parse_duration
from cron_deadman.server import make_handler, metrics


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


CONFIG = {
    "webhook": None,
    "checks": [{"name": "backup", "period": 3600, "grace": 600}, {"name": "report", "period": 60, "grace": 0}],
}


@pytest.mark.parametrize("raw,secs", [("90s", 90), ("15m", 900), ("1h30m", 5400), ("1d", 86400), ("2w", 1209600)])
def test_parse_duration(raw, secs):
    assert parse_duration(raw) == secs
    assert parse_duration(secs) == secs


@pytest.mark.parametrize("bad", ["", "abc", "1x", "1h junk"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_fmt_duration():
    assert fmt_duration(None) == "never"
    assert fmt_duration(45) == "45s"
    assert fmt_duration(5400) == "1h30m"
    assert fmt_duration(86400) == "1d"


def test_load_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("webhook: http://x\nchecks:\n  - name: backup\n    period: 1d\n    grace: 1h\n")
    assert load_config(p) == {"webhook": "http://x", "checks": [{"name": "backup", "period": 86400, "grace": 3600}]}
    p.write_text("checks:\n  - name: bad/name\n    period: 1d\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_lifecycle_down_and_recover():
    clock = Clock()
    store = Store(CONFIG, now=clock)
    assert store.evaluate() == []  # new checks get one full period + grace
    clock.t += 3600 + 601
    trans = store.evaluate()
    assert [(c.name, old, new) for c, old, new in trans] == [("backup", NEW, DOWN), ("report", NEW, DOWN)]
    assert store.evaluate() == []  # alert only once

    trans = store.ping("backup")
    assert [(old, new) for _, old, new in trans] == [(DOWN, UP)]
    clock.t += 3600
    assert store.evaluate() == []  # within grace
    clock.t += 601
    assert [c.name for c, _, _ in store.evaluate()] == ["backup"]


def test_fail_ping_and_duration():
    clock = Clock()
    store = Store(CONFIG, now=clock)
    store.ping("backup", "start")
    clock.t += 42
    trans = store.ping("backup", "fail", 3)
    check = store.checks["backup"]
    assert trans[0][2] == FAILED and check.last_exit_code == 3 and check.last_duration == 42
    assert store.evaluate() == []  # FAILED stays failed until a success ping
    assert store.ping("backup")[0][2] == UP


def test_unknown_check():
    with pytest.raises(KeyError):
        Store(CONFIG).ping("nope")


def test_state_survives_restart(tmp_path):
    state = tmp_path / "state.json"
    clock = Clock()
    Store(CONFIG, state, now=clock).ping("backup")
    clock.t += 10
    store = Store({"webhook": None, "checks": [{"name": "backup", "period": 7200, "grace": 0}]}, state, now=clock)
    c = store.checks["backup"]
    assert c.status == UP and c.last_ping == 1_000_000.0 and c.period == 7200  # config wins over saved period
    assert "report" not in store.checks


def test_metrics_format():
    clock = Clock()
    store = Store(CONFIG, now=clock)
    store.ping("backup")
    text = metrics(store)
    assert 'deadman_check_up{check="backup"} 1' in text
    assert 'deadman_check_up{check="report"} 0' in text
    assert 'deadman_last_ping_timestamp_seconds{check="backup"} 1000000.0' in text


@pytest.fixture
def server():
    alerts = []
    store = Store(CONFIG, now=Clock())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, alerts.append, token="s3cret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", store, alerts
    httpd.shutdown()


def get(url, token="s3cret", method="GET"):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_endpoints(server):
    base, store, alerts = server
    assert get(f"{base}/healthz", token=None) == (200, "ok\n")
    assert get(f"{base}/ping/backup", token=None)[0] == 401
    assert get(f"{base}/ping/backup", token="wrong")[0] == 401
    assert get(f"{base}/ping/backup?token=s3cret", token=None)[0] == 200
    assert get(f"{base}/ping/missing")[0] == 404
    assert get(f"{base}/ping/backup/bogus")[0] == 404
    assert get(f"{base}/ping/backup/fail?code=x")[0] == 400
    assert get(f"{base}/ping/backup/fail?code=2", method="POST")[0] == 200
    assert alerts == ["FAILED: 'backup' reported failure (exit code 2)"]
    get(f"{base}/ping/backup")
    assert alerts[-1] == "RECOVERED: 'backup' is back up"
    status = json.loads(get(f"{base}/status")[1])
    assert {s["name"]: s["status"] for s in status} == {"backup": UP, "report": NEW}
    assert "deadman_check_up" in get(f"{base}/metrics")[1]


def test_run_job_reports_success_and_failure(server, monkeypatch):
    base, store, alerts = server
    monkeypatch.setenv("DEADMAN_TOKEN", "s3cret")
    assert main(["run", f"{base}/ping/backup", "--", sys.executable, "-c", "import sys; sys.exit(0)"]) == 0
    assert store.checks["backup"].status == UP and store.checks["backup"].last_duration is not None
    assert main(["run", f"{base}/ping/backup", "--", sys.executable, "-c", "import sys; sys.exit(5)"]) == 5
    assert store.checks["backup"].last_exit_code == 5 and alerts[-1].startswith("FAILED")


def test_run_job_survives_monitor_outage(capsys):
    assert run_job("http://127.0.0.1:9/ping/x", [sys.executable, "-c", "print('job ran')"]) == 0
    captured = capsys.readouterr()
    assert "could not ping" in captured.err


def test_run_job_missing_command():
    assert run_job("http://127.0.0.1:9/ping/x", ["/nonexistent/binary"]) == 127
    assert main(["run", "http://127.0.0.1:9/ping/x"]) == 2

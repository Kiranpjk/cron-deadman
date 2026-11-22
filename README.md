# cron-deadman

[![CI](https://github.com/Kiranpjk/cron-deadman/actions/workflows/ci.yml/badge.svg)](https://github.com/Kiranpjk/cron-deadman/actions/workflows/ci.yml)

A self-hosted dead man's switch for cron jobs and scheduled tasks. Jobs ping it when they finish;
if a ping doesn't arrive in time, or a job reports failure, you get an alert. One small Python
service with a JSON state file, a Slack-compatible webhook and Prometheus metrics.

## The problem

Cron fails silently. If the backup script crashes, the cron daemon stops, the server is
rebuilt without its crontab, or the job just hangs, nothing tells you. Log-based alerting only
fires when something *happens*, but here the problem is that something *didn't* happen.

Community threads describing it:

- Discourse Meta, "Backups are no longer happening" (Apr 2021): a self-hosted instance's
  automatic backups silently stopped about a month before anyone noticed, with no error alert.
  <https://meta.discourse.org/t/backups-are-no-longer-happening/185847>
- Unraid Forums, "Cronjob stopped running": a scheduled backup job quietly stopped firing even
  though it worked when started by hand and syslog showed nothing.
  <https://forums.unraid.net/topic/97213-cronjob-stopped-running/>
- Hacker News, "Ask HN: How to do simple heartbeat monitoring?" (May 2024): alerts that only
  fire on errors missed a full outage; replies recommend dead man's switch style checks.
  <https://news.ycombinator.com/item?id=40276687>

> Research note: the brief asked for Reddit threads (r/sysadmin, r/selfhosted). Reddit was not
> reachable from the environment this was built in (blocked by the network proxy, and not
> returned by the search tool), so these are the closest verified public threads.

## How it works

Each check has a `period` (how often the job runs) and a `grace` (how late it may be).

| Status   | Meaning |
|----------|---------|
| `new`    | No ping yet; gets one period + grace from first startup before it counts as missed |
| `up`     | Last run succeeded and the next one isn't overdue yet |
| `failed` | The job reported failure (`/fail`), alerts straight away |
| `down`   | No ping within period + grace, alerts once |

Alerts go out when a check **changes** status (down, failed, recovered), so a dead job doesn't
spam you every 30 seconds. State is saved atomically to a JSON file, so restarts keep history.

### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET/POST /ping/<name>` | job finished successfully |
| `GET/POST /ping/<name>/start` | job started (enables duration tracking) |
| `GET/POST /ping/<name>/fail?code=N` | job failed with exit code N |
| `GET /status` | JSON status of every check |
| `GET /metrics` | Prometheus metrics (`deadman_check_up`, `deadman_last_ping_timestamp_seconds`, `deadman_last_duration_seconds`) |
| `GET /healthz` | liveness probe |

If `DEADMAN_TOKEN` is set, ping endpoints need `Authorization: Bearer <token>` or `?token=<token>`.

## Demo

Real local run. The server ran with the config below and a 2-second watcher interval, and a small
local HTTP server stood in for the Slack webhook.

```yaml
# checks.yaml
webhook: http://127.0.0.1:9900/hook   # Slack-compatible incoming webhook
checks:
  - name: nightly-backup
    period: 1d
    grace: 1h
  - name: db-vacuum
    period: 10s      # tiny values so the demo finishes quickly
    grace: 5s
```

```console
$ cron-deadman serve -c checks.yaml --state state.json --port 8099 --interval 2 &

$ cron-deadman run http://127.0.0.1:8099/ping/nightly-backup -- ./backup.sh
backing up
$ echo $?
0
$ cron-deadman run http://127.0.0.1:8099/ping/db-vacuum -- ./vacuum.sh
vacuum failed
$ echo $?
3
$ curl -s http://127.0.0.1:8099/ping/db-vacuum
ok
# ... 20 seconds pass without another db-vacuum ping ...
$ curl -s http://127.0.0.1:8099/metrics
# HELP deadman_check_up 1 if the check pinged successfully within period+grace.
# TYPE deadman_check_up gauge
deadman_check_up{check="nightly-backup"} 1
deadman_check_up{check="db-vacuum"} 0
# HELP deadman_last_ping_timestamp_seconds Unix time of the last finished run.
# TYPE deadman_last_ping_timestamp_seconds gauge
deadman_last_ping_timestamp_seconds{check="nightly-backup"} 1795391693.6808608
deadman_last_ping_timestamp_seconds{check="db-vacuum"} 1795391693.8643584
# HELP deadman_last_duration_seconds Duration of the last run (needs a /start ping).
# TYPE deadman_last_duration_seconds gauge
deadman_last_duration_seconds{check="nightly-backup"} 1.004450798034668
```

Server log:

```text
2026-11-23 00:54:50,755 INFO listening on http://127.0.0.1:8099 with 2 check(s)
2026-11-23 00:54:53,801 WARNING FAILED: 'db-vacuum' reported failure (exit code 3)
2026-11-23 00:54:53,864 WARNING RECOVERED: 'db-vacuum' is back up
2026-11-23 00:55:10,758 WARNING DOWN: 'db-vacuum' is overdue (last ping 16s ago; expected every 10s)
```

The webhook got the same three messages, each prefixed with `cron-deadman:`. `/status` showed
`nightly-backup` as `up` with `last_duration` of 1.0 s, and `db-vacuum` as `down`.

## Install

```bash
pip install git+https://github.com/Kiranpjk/cron-deadman.git
```

Requires Python 3.10+ and PyYAML. The `run` wrapper only uses the standard library.

## Usage

### 1. Start the server

```bash
cp checks.example.yaml checks.yaml   # edit names, periods and the webhook
cron-deadman serve -c checks.yaml --state /var/lib/cron-deadman/state.json --host 0.0.0.0 --port 8080
```

### 2. Report from your jobs

Wrap the command (sends `/start`, then success or `/fail?code=N`, and keeps the job's exit code):

```cron
0 2 * * * cron-deadman run http://monitor:8080/ping/nightly-backup -- /usr/local/bin/backup.sh
```

Or ping with plain curl from any machine, no install needed:

```cron
0 2 * * * /usr/local/bin/backup.sh && curl -fsS -m 10 --retry 3 http://monitor:8080/ping/nightly-backup
```

If the monitor is unreachable, `run` prints a warning but still runs the job and returns its
real exit code: a monitoring outage never breaks the job.

### 3. Scrape or alert

Point Prometheus at `/metrics` and alert on `deadman_check_up == 0`, or rely on the built-in
webhook (Slack, Mattermost, or anything that accepts `{"text": "..."}`).

## Run with Docker

```bash
docker build -t cron-deadman .
docker run -d --name cron-deadman -p 8080:8080 \
  -v "$PWD/checks.yaml:/config/checks.yaml:ro" -v deadman-data:/data \
  ghcr.io/kiranpjk/cron-deadman:main
```

The image runs as a non-root user, keeps state in the `/data` volume and has a `HEALTHCHECK`
against `/healthz`.

## CI/CD

- `.github/workflows/ci.yml`: `ruff check`, `ruff format --check`, `pytest` on Python 3.10 to 3.13
  (the tests run the real HTTP server and wrap real subprocesses), then a Docker build plus
  `--version` smoke test.
- `.github/workflows/release.yml`: on pushes to `main` and `v*` tags, builds a multi-arch
  (amd64/arm64) image and pushes it to `ghcr.io/kiranpjk/cron-deadman` with the built-in `GITHUB_TOKEN`.
- `.github/dependabot.yml`: weekly updates for pip, GitHub Actions and the Docker base image.

## Limitations

- Schedules are expressed as a fixed period, not a cron expression.
- One process with a JSON file: fine for tens or hundreds of checks, not built for HA.

## Development

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest -v
```

## License

MIT, see [LICENSE](LICENSE).

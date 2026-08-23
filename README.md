# DeviantArt Daily Deviation Watcher

Polls the DeviantArt Daily Deviations feed on a schedule, works out which
deviations it has not seen before, and pushes each one to your phone.

Runs as a Kubernetes CronJob. Ships with metrics, a Grafana dashboard, and a
staleness alert for the failure that actually matters: a scheduled job that
quietly stops running.

## How it works

```
CronJob ─▶ dawatch run ─┬─▶ DeviantArt API   (client_credentials)
                        ├─▶ SQLite on a PVC  (what has been seen)
                        ├─▶ ntfy             (your phone)
                        └─▶ Pushgateway ─▶ Prometheus ─▶ Grafana
```

`WatchService` depends on four protocols — `DeviationSource`, `SeenStore`,
`Notifier`, `MetricsSink` — and on nothing else. Every behaviour the product
promises is tested against fakes, with no network, no clock, and no
filesystem.

## Two decisions worth explaining

**A deviation is marked seen only after its notification succeeds.** That
gives at-least-once delivery: a crash between the two causes one duplicate on
the next run. The reverse ordering gives at-most-once, where the same crash
loses the deviation permanently and silently. A duplicate buzz is an
annoyance; a missed deviation defeats the product.

**An empty store is seeded, not notified.** A first deployment would otherwise
fire twenty notifications at once, which is enough to make someone uninstall
the app. `dawatch run` seeds silently when the store is empty; `--no-seed`
overrides it.

## Quick start (local, no cluster)

```bash
cd script
cp .env.example .env       # add your DeviantArt app credentials and ntfy topic
uv sync --all-groups
uv run dawatch doctor      # verifies credentials, API, store and gateway
uv run dawatch run --dry-run
uv run dawatch run
```

Install the [ntfy app](https://ntfy.sh/) on your phone and subscribe to the
topic you set in `.env`. Treat the topic name as a secret: on the public
ntfy.sh server, anyone who knows it can read your notifications.

Register an application at
https://www.deviantart.com/developers/apps to get a client ID and secret.

## Quick start (Kubernetes)

```bash
cp k8s/secret.example.yaml k8s/secret.yaml   # then edit in real values
make demo                                    # cluster, image, deploy, run, logs
make grafana                                 # http://localhost:3000
```

`make help` lists every target. The local overlay runs the CronJob every five
minutes and deploys Pushgateway, Prometheus and Grafana alongside it; the prod
overlay keeps the daily schedule and expects those to already exist.

## Commands

| Command | What it does |
|---|---|
| `dawatch run` | Poll once, notify what is new, exit |
| `dawatch run --dry-run` | Show what would be sent; send and write nothing |
| `dawatch run --date 2026-08-01` | Poll a specific day |
| `dawatch run --no-seed` | Notify everything even on an empty store |
| `dawatch seed` | Record today's feed as seen without notifying |
| `dawatch doctor` | Check credentials, API, store and gateway |

Exit codes: `0` success, `1` transient or partial failure, `2` configuration
failure. The CronJob's `backoffLimit` retries; a `2` will never succeed on
retry and tells you to fix the deployment.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DEVIANTART_CLIENT_ID` | — | Required. OAuth2 application ID |
| `DEVIANTART_CLIENT_SECRET` | — | Required. OAuth2 application secret |
| `DAWATCH_NTFY_TOPIC` | — | Required. ntfy topic to publish to |
| `DAWATCH_NTFY_URL` | `https://ntfy.sh` | ntfy server |
| `DAWATCH_DB_PATH` | `/data/dawatch.db` | SQLite location |
| `DAWATCH_ENV` | `prod` | `dev` switches logs to a console renderer |
| `DAWATCH_LOG_LEVEL` | `INFO` | Standard logging levels |
| `DAWATCH_PUSHGATEWAY_URL` | unset | Unset disables metrics entirely |
| `DAWATCH_HTTP_TIMEOUT` | `10.0` | Seconds |
| `DAWATCH_MAX_RETRIES` | `3` | Attempts per API request |

Values come from the environment, or from `script/.env` when running locally.
In the cluster, credentials arrive from the `dawatch-secrets` Secret and
everything else from the `dawatch-config` ConfigMap.

## Operational notes

The PVC must be backed by local-path or block storage. SQLite's advisory
locking is unreliable over NFS and can corrupt the database. The CronJob sets
`concurrencyPolicy: Forbid`, so exactly one process holds the file at a time.

Metrics are pushed at the end of every run, successful or not, so a failing
run still moves `dawatch_errors_total`. A push failure is logged and never
fatal.

The alert to wire up is staleness:

```
time() - dawatch_last_success_timestamp_seconds > 172800
```

A job that stops running produces no errors and no notifications. Nothing
else catches it. Both this alert and an hourly error alert ship in
`k8s/observability/prometheus.yaml`.

## Development

```bash
make test    # pytest with coverage (fails under 90%)
make lint    # ruff + mypy --strict
make image   # build the container
```

Python 3.13 or newer. Dependencies and the virtualenv are managed by `uv`.

## Not implemented

The "artists you watch" feed needs the OAuth2 authorization code flow — user
consent in a browser and refresh-token storage — rather than the client
credentials grant used here. It would arrive as a second `DeviationSource`
without any change to the orchestration layer.

There is no CI pipeline yet. `make lint && make test` is the gate.

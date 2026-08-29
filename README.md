# DeviantArt Watcher

Polls the deviations posted by the artists you watch, works out which ones it
has not seen before, and pushes each one to your phone.

Runs as a Kubernetes CronJob. Ships with metrics, a Grafana dashboard, and a
staleness alert for the failure that actually matters: a scheduled job that
quietly stops running.

## How it works

```
CronJob ─▶ dawatch run ─┬─▶ DeviantArt API   (refresh_token grant)
                        ├─▶ SQLite on a PVC  (what has been seen)
                        ├─▶ ntfy             (your phone)
                        └─▶ Pushgateway ─▶ Prometheus ─▶ Grafana
```

`WatchService` depends on four protocols — `DeviationSource`, `SeenStore`,
`Notifier`, `MetricsSink` — and on nothing else. Every behaviour the product
promises is tested against fakes, with no network, no clock, and no
filesystem.

## Three decisions worth explaining

**A deviation is marked seen only after its notification succeeds.** That
gives at-least-once delivery: a crash between the two causes one duplicate on
the next run. The reverse ordering gives at-most-once, where the same crash
loses the deviation permanently and silently. A duplicate buzz is an
annoyance; a missed deviation defeats the product.

**The refresh token is seeded from OpenBao and rotated into SQLite.** The app
prefers a token persisted in `token_cache` on the PVC and falls back to the
`DAWATCH_REFRESH_TOKEN` environment seed. Any refresh token the endpoint
returns is written back. DeviantArt does not document whether refresh tokens
rotate on use, and this arrangement is correct either way — while leaving
External Secrets a read-only sync, and degrading to the seed rather than to a
browser prompt if the volume is lost.

**An empty store is seeded, not notified.** A first deployment would otherwise
fire twenty notifications at once, which is enough to make someone uninstall
the app. `dawatch run` seeds silently when the store is empty; `--no-seed`
overrides it.

## Quick start (local, no cluster)

```bash
cd script
cp .env.example .env       # add your DeviantArt app credentials and ntfy topic
uv sync --all-groups
uv run dawatch login       # browser consent; prints DAWATCH_REFRESH_TOKEN
uv run dawatch doctor      # verifies credentials, scope, store and gateway
uv run dawatch run --dry-run
uv run dawatch run
```

Put the printed refresh token in `.env` as `DAWATCH_REFRESH_TOKEN` before
running anything else. `dawatch login` needs `http://localhost:8080/callback`
registered as a redirect URI on your DeviantArt application.

Install the [ntfy app](https://ntfy.sh/) on your phone and subscribe to the
topic you set in `.env`. Treat the topic name as a secret: on the public
ntfy.sh server, anyone who knows it can read your notifications.

Register an application at
https://www.deviantart.com/developers/apps to get a client ID and secret, and
add `http://localhost:8080/callback` to its redirect URI whitelist.

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
| `dawatch login` | Authorize against your account; print a refresh token |
| `dawatch run` | Poll once, notify what is new, exit |
| `dawatch run --dry-run` | Show what would be sent; send and write nothing |
| `dawatch run --no-seed` | Notify everything even on an empty store |
| `dawatch seed` | Record the current feed as seen without notifying |
| `dawatch doctor` | Check credentials, scope, store and gateway |

There is no `--date`. The watch feed is an offset-paged stream with no date
parameter; each run pages back until it meets a deviation it has already seen,
capped at five pages.

Exit codes: `0` success, `1` transient or partial failure, `2` configuration
failure. The CronJob's `backoffLimit` retries; a `2` will never succeed on
retry and tells you to fix the deployment.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DEVIANTART_CLIENT_ID` | — | Required. OAuth2 application ID |
| `DEVIANTART_CLIENT_SECRET` | — | Required. OAuth2 application secret |
| `DAWATCH_REFRESH_TOKEN` | — | Seed token from `dawatch login`. Expires after 3 months |
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

The refresh token expires after three months, and DeviantArt offers no way to
renew it without a human in a browser. That is the one scheduled failure this
design cannot remove. It surfaces as `invalid_grant`, which exits **2** rather
than 1 so `backoffLimit` stops retrying it, and then as the staleness alert
below. The fix is one `dawatch login` and one write to OpenBao.

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

There is no CI pipeline yet. `make lint && make test` is the gate.

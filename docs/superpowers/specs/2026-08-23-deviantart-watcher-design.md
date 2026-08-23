# DeviantArt Daily Deviation Watcher — Design

**Date:** 2026-08-23
**Status:** Approved for planning

## Purpose

Poll the DeviantArt Daily Deviations feed on a schedule, detect deviations not
seen on a previous run, and push each one to the user's phone via ntfy.

The project doubles as a portfolio piece. It is therefore built to production
standards — typed, tested, observable, containerised, and deployed as a
Kubernetes CronJob — rather than as a single script.

## Success criteria

1. A new Daily Deviation reaches the phone within one scheduling interval.
2. No deviation is ever notified twice under normal operation.
3. No deviation is ever silently dropped, including when the process crashes
   mid-run.
4. A first deployment against an empty store does not emit a burst of
   notifications.
5. Failure of the scheduled job is detectable without watching for missing
   notifications.
6. `git clone && make demo` brings up a working cluster, job, and dashboard.

## Out of scope

- `frontend/` is untouched.
- The "artists you watch" feed. It requires the OAuth2 authorization code
  flow, which needs user consent and refresh-token storage. The source
  abstraction below leaves room for it as a later addition.
- Downloading or archiving artwork. Notifications link out to DeviantArt.

## External API

### Authentication

`POST https://www.deviantart.com/oauth2/token`

Parameters: `client_id`, `client_secret`, `grant_type=client_credentials`.

The response carries `access_token`, `token_type: "Bearer"`, and
`expires_in: 3600`. The client credentials grant issues **no refresh token**;
an expired token is replaced by repeating the same request. Required scope for
the feed is `browse`, which this grant provides.

### Feed

`GET https://www.deviantart.com/api/v1/oauth2/browse/dailydeviations`

The only meaningful query parameter is `date` (`YYYY-MM-DD`, defaulting to
today). **The endpoint has no `limit` or `offset` parameter** — it returns a
whole day's picks in one response. The design therefore has no pagination
loop and no page-size flag.

Requests pin the API version by sending the `dA-minor-version: 20240701`
header, so a future server-side default change cannot alter the response shape
without a deliberate code change.

Fields consumed from each result: `deviationid` (the dedupe key), `title`,
`url`, `author.username`, `is_mature`, `published_time`, and
`preview.src` / `content.src` for the notification image. Every other field is
ignored; models tolerate unknown keys so that API additions do not break the
job.

### Token validation

`GET /api/v1/oauth2/placebo` confirms a token is live. It backs the `doctor`
command and is not called on the normal path.

## Architecture

Four protocols form the seams. Each has a production implementation and a test
double, so the orchestration layer is exercised without a network, a clock, or
a filesystem.

| Protocol | Production | Test double |
|---|---|---|
| `DeviationSource` | `DailyDeviationsSource` | `FakeSource` |
| `SeenStore` | `SqliteSeenStore` | `InMemorySeenStore` |
| `Notifier` | `NtfyNotifier` | `RecordingNotifier`, `ConsoleNotifier` |
| `MetricsSink` | `PushgatewaySink` | `NullSink` |

`WatchService` depends on the four protocols and nothing else. Adding the
watch feed later means writing one new `DeviationSource`; swapping ntfy for
Telegram means one new `Notifier`. Neither touches the orchestration.

### Repository layout

```
devientArt/
├── Makefile                 # build, kind-up, load, apply, test, demo
├── README.md
├── .github/workflows/ci.yml
├── docs/superpowers/specs/
├── frontend/                # out of scope
├── k8s/
│   ├── base/                # namespace, configmap, pvc, cronjob, kustomization
│   ├── overlays/local/      # kind: local-path SC, :dev tag, frequent schedule
│   ├── overlays/prod/       # real SC, pinned digest, daily schedule
│   ├── observability/       # pushgateway, prometheus, grafana + dashboard CM
│   └── secret.example.yaml
└── script/
    ├── pyproject.toml
    ├── Dockerfile           # build context is script/
    ├── .env.example
    ├── src/dawatch/
    │   ├── config.py        # Settings (pydantic-settings)
    │   ├── logging.py       # structlog config + secret redaction
    │   ├── models.py        # Deviation, DailyDeviationsPage, Token
    │   ├── errors.py        # ConfigError, AuthError, FetchError, NotifyError
    │   ├── auth.py          # client_credentials token, cached
    │   ├── client.py        # httpx client, backoff, Retry-After
    │   ├── sources.py       # DeviationSource, DailyDeviationsSource
    │   ├── store.py         # SeenStore, SqliteSeenStore, InMemorySeenStore
    │   ├── notifier.py      # Notifier, NtfyNotifier, ConsoleNotifier
    │   ├── metrics.py       # MetricsSink, PushgatewaySink, NullSink
    │   ├── service.py       # WatchService.run()
    │   └── cli.py           # run | seed | doctor
    └── tests/
```

### Data flow

```
cli → Settings.load() → structlog.bind(run_id=uuid4())
  │
  └─ WatchService.run(date)
       1. token = auth.token()            # cached; re-request if expired
       2. page  = source.fetch(date)      # GET browse/dailydeviations
       3. new   = [d for d in page.results if not store.seen(d.deviationid)]
       4. for d in new:
              notifier.send(d)            # POST ntfy
              store.mark_seen(d)          # AFTER a successful send
       5. metrics.flush()                 # push to pushgateway
```

### Delivery ordering

Step 4 marks a deviation seen only after its notification succeeds. This
yields at-least-once delivery: a crash between send and mark causes exactly
one duplicate notification on the next run.

The reverse order would yield at-most-once, where the same crash loses the
deviation permanently and silently. A duplicate buzz is an annoyance; a missed
deviation defeats the product. The trade is deliberate.

### Seeding

An empty store treats every deviation in the day's feed as new, so a first
deployment would fire ten to thirty notifications at once.

`dawatch seed` records the current feed as seen without notifying. `dawatch
run` performs the same seeding automatically when it finds the store empty,
unless `--no-seed` is passed. The seeding path is covered by its own test.

### Token caching

The job is short-lived, so an in-process cache alone would re-authenticate on
every scheduled run. The token and its `expires_at` live in the same SQLite
file as the seen-store, and a token is reused while more than 60 seconds of
life remain. A daily schedule therefore authenticates roughly once per run; a
five-minute local schedule authenticates about once an hour.

### Storage

SQLite, via the standard library, at `/data/dawatch.db`.

```sql
CREATE TABLE seen_deviations (
    deviationid  TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    author       TEXT NOT NULL,
    url          TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    notified_at   TEXT
);
CREATE TABLE token_cache (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    access_token TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
```

Connections open with `journal_mode=WAL` and `busy_timeout=5000`. Schema is
created idempotently on startup; a `schema_version` pragma guards future
migrations.

The store is mounted from a `PersistentVolumeClaim`, because a CronJob pod's
filesystem does not survive the run. The claim is `ReadWriteOnce` and the
CronJob sets `concurrencyPolicy: Forbid`, so exactly one process holds the
database at any time.

**The claim must be backed by local-path or block storage, never NFS.**
SQLite's advisory locking is unreliable over NFS and can corrupt the file.

## Error handling

| Failure | Behaviour | Exit |
|---|---|---|
| Missing or invalid configuration | fail before any HTTP call | `2` |
| Auth rejected | `errors{stage="auth"}`, abort run | `1` |
| Fetch returns 429 or 5xx | exponential backoff honouring `Retry-After`, then abort | `1` |
| A single notification fails | log, leave unseen, continue to next deviation | `1` |
| SQLite unavailable or corrupt | fatal | `1` |
| Clean run | flush metrics | `0` |

Distinguishing `2` (configuration, will never succeed on retry) from `1`
(transient) lets the CronJob's `backoffLimit` retry only what is worth
retrying.

Backoff applies to connection errors, 429, and 5xx. It never applies to 4xx
other than 429, which indicate a defect rather than a transient fault.

## Security

- Credentials arrive as environment variables from a Kubernetes `Secret`.
  `k8s/secret.example.yaml` is committed with placeholder values; the real
  secret is never in git.
- A structlog processor redacts `client_secret`, `access_token`, and
  `Authorization` from every event, so a token cannot reach a log line even
  through an exception repr.
- `Settings` types secrets as `SecretStr`, so an accidental `repr` prints
  `**********`.
- The container runs as a non-root user with `readOnlyRootFilesystem: true`,
  `allowPrivilegeEscalation: false`, all capabilities dropped, and
  `seccompProfile: RuntimeDefault`. `/data` is the only writable mount.

## Observability

### Logging

`structlog` wrapping stdlib `logging`, so `httpx`'s own loggers are captured.
JSON to stdout when `DAWATCH_ENV=prod`; a colourised console renderer in dev.
Every event carries the `run_id` bound at startup.

### Metrics

`prometheus_client` pushes to a Pushgateway at the end of each run, grouped by
job name. A CronJob pod is unscrapeable — it has usually exited before
Prometheus notices it — which is precisely the case the Pushgateway exists to
serve.

```
dawatch_last_success_timestamp_seconds            gauge
dawatch_run_duration_seconds                      histogram
dawatch_deviations_fetched_total                  counter
dawatch_notifications_sent_total                  counter
dawatch_errors_total{stage="auth|fetch|notify|store"}  counter
```

`dawatch_last_success_timestamp_seconds` is the important one. The failure
mode that matters is a job that quietly stops running, which produces no
errors and no notifications and is otherwise invisible. The alert rule is
staleness:

```
time() - dawatch_last_success_timestamp_seconds > 172800
```

### Dashboard

A Grafana dashboard is provisioned from JSON held in a ConfigMap, so it is
version-controlled rather than clicked together. Panels: run outcome over
time, notifications sent, error rate by stage, and time since last success.

## Deployment

The image is built from `script/`, tagged, and loaded into a `kind` cluster
for local work. Manifests are Kustomize: a `base` plus `local` and `prod`
overlays differing in image tag, schedule, and StorageClass.

CronJob settings that matter:

| Setting | Value | Reason |
|---|---|---|
| `concurrencyPolicy` | `Forbid` | one SQLite writer at a time |
| `startingDeadlineSeconds` | `300` | skip a missed run rather than stampede |
| `backoffLimit` | `2` | retry transient faults, not config faults |
| `ttlSecondsAfterFinished` | `3600` | reap finished Jobs |
| `successfulJobsHistoryLimit` | `3` | bounded history for debugging |
| `failedJobsHistoryLimit` | `3` | keep failures around to inspect |
| resource requests/limits | set | schedulable and not noisy |

`docker-compose` is deliberately absent. It would describe the same four
services a second time, and two deployment paths drift apart. `make dev`
against kind is the local loop.

## Testing

Test-driven: each module's tests are written before its implementation.

- **Unit.** `respx` mocks httpx for `auth`, `client`, `sources`, and
  `notifier`. `time-machine` drives token expiry. `SqliteSeenStore` is tested
  against a real temporary database file, not a mock, since its correctness is
  the point.
- **`test_service.py`** is the centre of the suite. Using the four fakes, it
  covers: an empty feed notifies nothing; an all-seen feed notifies nothing;
  a partially seen feed notifies only the new items; a notification failure
  leaves that deviation unseen while later ones still process; a crash after
  send is not re-notified; an empty store seeds silently.
- **Integration.** `run()` end to end against respx, a temporary database, and
  a stub ntfy endpoint, asserting exit codes.
- **Cluster.** CI creates a `kind` cluster, loads the image, applies the local
  overlay, triggers the CronJob manually, and asserts the Job reaches
  `Complete` with the expected log output.

CI runs `ruff check`, `ruff format --check`, `mypy --strict`, and `pytest
--cov` with a coverage floor, on Python 3.13 and 3.14.

## Dependencies

Runtime: `httpx`, `pydantic`, `pydantic-settings`, `structlog`,
`prometheus-client`. Development: `pytest`, `pytest-cov`, `respx`,
`time-machine`, `ruff`, `mypy`. Locked with `uv`.

## CLI

```
dawatch run    [--date YYYY-MM-DD] [--dry-run] [--no-seed]
dawatch seed   [--date YYYY-MM-DD]
dawatch doctor
```

`run` performs one poll and exits; the schedule lives in Kubernetes, not in
the process. `--dry-run` fetches and diffs but neither notifies nor writes.
`doctor` validates configuration, calls `/placebo`, checks the database is
writable, and pings the Pushgateway — one command to answer "is this
deployment healthy".

## Open item

The repository is not yet under version control. `git init` should happen
before implementation begins, since the CI workflow, the commit history, and
the README are all part of what makes this a portfolio piece.

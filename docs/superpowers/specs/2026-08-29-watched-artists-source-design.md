# Watched Artists Source

**Status:** approved design, not yet implemented
**Date:** 2026-08-29

## Problem

`dawatch` notifies on Daily Deviations — the staff-picked, site-wide feed. That
is not what the user asked for. They want deviations from the artists they
watch on their own account.

The two feeds need different identities. `client_credentials` authenticates the
*application*; it has no user, so it cannot know who anyone watches. The watch
feed requires the `authorization_code` grant, which means a human consenting in
a browser and a refresh token that outlives the process.

## Decisions

**The watch feed replaces Daily Deviations in the run path.** One source, one
notification stream. `DailyDeviationsSource` and its tests stay in the tree:
they cost nothing, they document the Protocol's second implementation, and the
README's "artists you watch would arrive as a second `DeviationSource`" claim
is only demonstrated if both exist. Nothing schedules it.

**The refresh token is seeded from OpenBao and rotated into SQLite.** At startup
the app prefers a refresh token persisted in `token_cache`, falling back to the
`DAWATCH_REFRESH_TOKEN` environment seed that External Secrets syncs from
OpenBao. After every refresh, any newly returned refresh token is written back
to SQLite.

DeviantArt does not document whether refresh tokens rotate on use. This design
does not need the answer: persisting whatever comes back is correct under both
behaviours, and needs no new infrastructure. External Secrets stays a read-only
sync, the PVC keeps the mutable copy, and losing the PVC degrades to the
OpenBao seed rather than to a browser prompt.

**Each run pages until it meets a deviation it has already seen, capped at five
pages.** A quiet run costs one request. A run after an outage catches up
without a date parameter to reason about. The cap bounds a first-run backfill.

## Constraint the design cannot remove

> "The `refresh_token` will expire after 3 months. After that time you must
> re-authorize the app."
> — DeviantArt authentication docs

This feed can never be fully unattended. Roughly quarterly, a human must
re-consent in a browser. The design's job is to make that a signposted
30-second operation rather than a silent outage, which is what the error
handling below is for.

## Components

### `RefreshTokenAuth` (new, `auth.py`)

Implements the existing `TokenProvider` Protocol, so `DeviantArtClient` and
`WatchService` never learn which grant is in play.

- Resolves a refresh token: `store.load_refresh_token()`, else the configured
  seed.
- `POST` to the existing `TOKEN_URL` with `grant_type=refresh_token`.
- Caches the resulting access token through the existing `TokenCache`
  machinery, unchanged.
- If the response carries a `refresh_token`, persists it via
  `store.save_refresh_token()`.

`DeviantArtAuth` (client credentials) is untouched.

`_doctor` currently annotates its parameter as the concrete `DeviantArtAuth`
and calls `auth.token()` before probing `/placebo`. It must widen to the
`TokenProvider` Protocol so the same health check covers whichever grant the
deployment actually runs on. This is the only signature change outside the new
code, and `mypy --strict` will catch it if missed.

### `WatchedDeviationsSource` (new, `sources.py`)

Satisfies the existing `DeviationSource` Protocol.

- `PATH = "browse/deviantsyouwatch"`
- Pages on `offset`, `limit=50`, reading `has_more` and `next_offset`.
- Stops at the first `deviationid` the store has already seen, or at
  `MAX_PAGES = 5`.
- Reuses `DailyDeviationsSource`'s per-row validation behaviour: one malformed
  row is logged and skipped, never fatal to the batch.

The stop rule needs to know what has been seen, but `fetch()` returns
everything and lets `WatchService` filter. Rather than widen the Protocol, the
source takes a `seen: Callable[[str], bool]` predicate in its constructor.
`SqliteStore.has_seen` satisfies it as-is, and tests supply a set-backed fake.

### `store.py`

`token_cache` gains one column:

```sql
CREATE TABLE IF NOT EXISTS token_cache (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    refresh_token TEXT
);
```

`SCHEMA` runs `CREATE TABLE IF NOT EXISTS`, so an existing PVC never sees the
new column. Startup performs an additive migration: read `PRAGMA
table_info(token_cache)`, and `ALTER TABLE ... ADD COLUMN refresh_token TEXT`
when absent. Nullable, so no backfill and no migration framework.

The `TokenCache` Protocol gains `load_refresh_token()` and
`save_refresh_token()`. `InMemoryStore` mirrors both.

The refresh token is deliberately *not* folded into the `Token` model. `Token`
carries an expiry the code reasons about every run; the refresh token has a
three-month life nothing tracks. Separate lifecycles, separate storage.

### `cli.py`

**`dawatch login`** (new): binds a one-shot listener on
`http://localhost:8080/callback`, opens the consent URL, captures the returned
`code`, exchanges it for a token pair, and prints the refresh token for the
operator to place in OpenBao. It writes nothing to the cluster — the operator
does that deliberately.

**`dawatch run --date` / `dawatch seed --date`**: the watch feed has no date
parameter. Passing `--date` must fail loudly with a message naming the reason,
never be silently ignored.

**`dawatch doctor`**: gains a refresh-token check — present, and still
accepted by the token endpoint.

### Configuration

`DAWATCH_REFRESH_TOKEN` joins `Settings`, the ExternalSecret in
`k8s/overlays/prod/secret.yaml`, and OpenBao at `secret/deviantart/config`.

`DEVIANTART_CLIENT_ID` and `DEVIANTART_CLIENT_SECRET` are still required: the
refresh grant authenticates the application alongside the user.

The DeviantArt app registration needs `http://localhost:8080/callback` added as
a redirect URI before `dawatch login` can work.

## Error handling

A dead or revoked refresh token returns HTTP 400 `invalid_grant`. This is a
**configuration** failure, not a transient one, and must exit **2**. Exit 1
would let `backoffLimit` retry something that can never succeed on retry.

The existing staleness alert then does the real work:

```
time() - dawatch_last_success_timestamp_seconds > 172800
```

Quarterly expiry surfaces as that alert firing within 48 hours, and the runbook
answer is a single `dawatch login`.

## Included fix

`notifier.py` passes `deviation.url` and `deviation.image_url` into the
`X-Click` and `X-Attach` headers raw, while only the title goes through
`_header_safe`. A non-ASCII URL makes httpx refuse to build the request,
raising `LocalProtocolError`.

This is already observed in production: the run on 2026-08-29 delivered 9 of 10
deviations and failed the tenth. Because a deviation is only marked seen after
its notification succeeds, the failure is a poison pill — it is retried every
run forever, pinning `dawatch_last_success_timestamp_seconds` at 0 and firing
the staleness alert while the system is otherwise healthy.

The fix percent-encodes rather than backslash-escapes, because the value is a
URL that must stay clickable:

```python
quote(deviation.url, safe=":/?#[]@!$&'()*+,;=~-._")
```

A watch list of international artists will hit this far harder than the Daily
Deviations feed did, so it is in scope here rather than deferred.

## Testing

The existing discipline holds: every behaviour tested against fakes, no
network, no clock, no filesystem.

- `RefreshTokenAuth`: seed-vs-store precedence; persists a rotated token;
  survives a response that omits `refresh_token`; maps `invalid_grant` to the
  config-failure type.
- `WatchedDeviationsSource`: stops at the first seen id; stops at the page cap;
  handles `has_more: false` mid-page; skips a malformed row without losing the
  batch.
- `store`: migration adds the column to a pre-existing database and is
  idempotent on a fresh one.
- `notifier`: a deviation whose URL contains non-ASCII characters produces a
  valid, still-clickable header rather than raising.
- `cli`: `--date` against the watch feed exits non-zero; `invalid_grant`
  produces exit 2, not 1.

Coverage gate stays at 90%.

## Out of scope

- Running both feeds at once. Rejected in favour of replacement.
- Writing rotated tokens back to OpenBao from the pod. Would need a
  ServiceAccount, policy, role, a client dependency, and
  `automountServiceAccountToken: true`. The SQLite path achieves the same
  durability with none of it.
- The `*/5` schedule. It exists for testing and should return to daily or
  hourly before this ships; a five-minute poll on a watch feed is almost
  entirely empty requests.

## Open question

The exact scope string for `browse/deviantsyouwatch` is unconfirmed — most
likely `browse`. DeviantArt's reference documentation moved to
`deviantart.readme.io` and the endpoint pages returned 404 during design.
`dawatch login` resolves it on first consent, since an insufficient scope fails
immediately and visibly. `doctor` should assert the granted scope so a
misconfiguration is caught before a scheduled run depends on it.

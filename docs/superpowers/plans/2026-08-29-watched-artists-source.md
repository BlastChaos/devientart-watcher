# Watched Artists Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Daily Deviations feed with deviations from the artists the user watches, authenticated by an OAuth2 refresh token that survives and rotates across short-lived CronJob runs.

**Architecture:** A new `RefreshTokenAuth` implements the existing `TokenProvider` Protocol so nothing downstream learns the grant changed. A new `WatchedDeviationsSource` implements the existing `DeviationSource` Protocol, paging until it meets an already-seen deviation. The refresh token is seeded from OpenBao through External Secrets and rotated into the existing SQLite `token_cache` table on the PVC.

**Tech Stack:** Python 3.13+, uv, httpx, pydantic + pydantic-settings, structlog, SQLite, pytest + respx + time_machine, ruff, mypy --strict.

**Spec:** `docs/superpowers/specs/2026-08-29-watched-artists-source-design.md`

## Global Constraints

- Python 3.13 or newer. Dependencies and virtualenv managed by `uv`; run everything as `uv run ...` from `script/`.
- `make lint` must pass: `ruff check .`, `ruff format --check .`, `mypy` (configured `--strict`).
- `make test` must pass with coverage at or above **90%** — the suite fails under it.
- Tests touch no network, no real clock, no real filesystem outside `tmp_path`. HTTP is faked with `respx`, time with `time_machine`, collaborators with hand-written stubs.
- Interfaces are `typing.Protocol`, never ABCs. Follow the existing `@runtime_checkable` Protocol style in `store.py`.
- Every error the application raises deliberately subclasses `DawatchError` from `errors.py`.
- Exit codes: `0` success, `1` transient or partial failure, `2` configuration failure that can never succeed on retry.
- Never log or include credential material in an exception message. `auth.py` deliberately omits response bodies for this reason; preserve that.

---

### Task 1: Fix the notifier's URL headers

Independent of everything else and fixes a live poison pill: a non-ASCII URL makes httpx refuse to build the request, the deviation is never marked seen, and it is retried forever. Do this first so the fix is available regardless of how the rest lands.

**Files:**
- Modify: `script/src/dawatch/notifier.py:19-25` (add helper), `script/src/dawatch/notifier.py:52-55` (use it)
- Test: `script/tests/test_notifier.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `dawatch.notifier._url_safe(value: str) -> str`. No other task depends on it.

- [ ] **Step 1: Write the failing tests**

Add to `script/tests/test_notifier.py`:

```python
def test_non_ascii_url_does_not_break_header_encoding() -> None:
    """A URL with non-ASCII characters must not raise LocalProtocolError.

    httpx encodes headers as latin-1. An unescaped non-ASCII URL makes it
    refuse to build the request at all, which previously left the deviation
    unseen and retried on every subsequent run.
    """
    deviation = Deviation(
        deviationid="X",
        title="Cafe",
        url="https://www.deviantart.com/user/art/Café-123",
        author=Author(username="alice"),
    )

    with respx.mock:
        route = respx.post("https://ntfy.sh/topic").mock(
            return_value=httpx.Response(200)
        )
        with httpx.Client() as http:
            NtfyNotifier(http, "https://ntfy.sh", "topic").send(deviation)

    sent = route.calls[0].request
    assert sent.headers["X-Click"] == "https://www.deviantart.com/user/art/Caf%C3%A9-123"


def test_already_encoded_url_is_not_double_encoded() -> None:
    """A percent-sign in an already-encoded URL must survive untouched."""
    deviation = Deviation(
        deviationid="X",
        title="Encoded",
        url="https://www.deviantart.com/user/art/Caf%C3%A9-123",
        author=Author(username="alice"),
    )

    with respx.mock:
        route = respx.post("https://ntfy.sh/topic").mock(
            return_value=httpx.Response(200)
        )
        with httpx.Client() as http:
            NtfyNotifier(http, "https://ntfy.sh", "topic").send(deviation)

    sent = route.calls[0].request
    assert sent.headers["X-Click"] == "https://www.deviantart.com/user/art/Caf%C3%A9-123"


def test_non_ascii_image_url_is_encoded() -> None:
    deviation = Deviation(
        deviationid="X",
        title="Art",
        url="https://example.com/a",
        author=Author(username="alice"),
        preview={"src": "https://images.example.com/ü.jpg"},
    )

    with respx.mock:
        route = respx.post("https://ntfy.sh/topic").mock(
            return_value=httpx.Response(200)
        )
        with httpx.Client() as http:
            NtfyNotifier(http, "https://ntfy.sh", "topic").send(deviation)

    sent = route.calls[0].request
    assert sent.headers["X-Attach"] == "https://images.example.com/%C3%BC.jpg"
```

Ensure the file's imports include `httpx`, `respx`, and `from dawatch.models import Author, Deviation`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd script && uv run pytest tests/test_notifier.py -k "non_ascii or double_encoded" -v`
Expected: FAIL. The first and third raise `httpx.LocalProtocolError`; the second fails only after the fix is written incorrectly, so it may pass at this point — that is fine, it is a regression guard.

- [ ] **Step 3: Implement**

In `script/src/dawatch/notifier.py`, add the import and helper next to `_header_safe`:

```python
from urllib.parse import quote

# Characters legal in a URL, plus '%' so an already-encoded URL is not
# double-encoded on its way into the header.
_URL_SAFE_CHARS = ":/?#[]@!$&'()*+,;=~-._%"


def _url_safe(value: str) -> str:
    """Percent-encode a URL for use as an HTTP header value.

    Titles use backslash-escaping because a mangled title is still readable.
    A URL is not: it has to stay clickable, so it is percent-encoded instead.
    """
    return quote(value, safe=_URL_SAFE_CHARS)
```

Then change the two header assignments:

```python
        if deviation.url:
            headers["X-Click"] = _url_safe(deviation.url)
        if deviation.image_url:
            headers["X-Attach"] = _url_safe(deviation.image_url)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd script && uv run pytest tests/test_notifier.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add script/src/dawatch/notifier.py script/tests/test_notifier.py
git commit -m "fix: percent-encode URLs in ntfy headers

A non-ASCII deviation URL made httpx refuse to build the request. Because a
deviation is only marked seen after a successful notification, the failure was
retried on every run forever, pinning last_success_timestamp at zero."
```

---

### Task 2: Persist a refresh token in the store

**Files:**
- Modify: `script/src/dawatch/store.py:18-34` (SCHEMA), `script/src/dawatch/store.py:62-67` (TokenCache Protocol), `script/src/dawatch/store.py:70-85` (`__init__`), and `SqliteStore` / `InMemoryStore` bodies
- Test: `script/tests/test_store.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `TokenCache.load_refresh_token() -> str | None` and `TokenCache.save_refresh_token(refresh_token: str) -> None`, implemented by both `SqliteStore` and `InMemoryStore`. Task 4 depends on both.

- [ ] **Step 1: Write the failing tests**

Add to `script/tests/test_store.py`:

```python
def test_refresh_token_round_trips(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "db.sqlite") as store:
        assert store.load_refresh_token() is None
        store.save_refresh_token("refresh-abc")
        assert store.load_refresh_token() == "refresh-abc"


def test_refresh_token_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    with SqliteStore(db) as store:
        store.save_refresh_token("refresh-abc")
    with SqliteStore(db) as reopened:
        assert reopened.load_refresh_token() == "refresh-abc"


def test_saving_an_access_token_preserves_the_refresh_token(tmp_path: Path) -> None:
    """The two credentials have independent lifecycles.

    An access token is replaced hourly; the refresh token must not be
    collaterally erased when that happens.
    """
    with SqliteStore(tmp_path / "db.sqlite") as store:
        store.save_refresh_token("refresh-abc")
        store.save_token(
            Token(access_token="access-xyz", expires_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        assert store.load_refresh_token() == "refresh-abc"
        loaded = store.load_token()
        assert loaded is not None
        assert loaded.access_token == "access-xyz"


def test_migrates_a_database_created_before_the_refresh_column(tmp_path: Path) -> None:
    """An existing PVC has a token_cache without refresh_token.

    CREATE TABLE IF NOT EXISTS will not add the column, so opening the store
    must add it. Without this, every deployment with an existing volume breaks
    on the first query.
    """
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE token_cache (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            access_token TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    with SqliteStore(db) as store:
        assert store.load_refresh_token() is None
        store.save_refresh_token("refresh-abc")
        assert store.load_refresh_token() == "refresh-abc"


def test_migration_is_idempotent_on_a_fresh_database(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    with SqliteStore(db):
        pass
    with SqliteStore(db) as store:
        assert store.load_refresh_token() is None


def test_in_memory_store_round_trips_a_refresh_token() -> None:
    store = InMemoryStore()
    assert store.load_refresh_token() is None
    store.save_refresh_token("refresh-abc")
    assert store.load_refresh_token() == "refresh-abc"
```

Ensure the file imports `sqlite3`, `from datetime import UTC, datetime`, `from pathlib import Path`, and `from dawatch.models import Token`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd script && uv run pytest tests/test_store.py -k refresh -v`
Expected: FAIL with `AttributeError: 'SqliteStore' object has no attribute 'load_refresh_token'`.

- [ ] **Step 3: Implement**

In `script/src/dawatch/store.py`, add the column to `SCHEMA` for fresh databases:

```python
CREATE TABLE IF NOT EXISTS token_cache (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    refresh_token TEXT
);
```

Extend the `TokenCache` Protocol:

```python
@runtime_checkable
class TokenCache(Protocol):
    """Persists an access token across short-lived process runs."""

    def load_token(self) -> Token | None: ...

    def save_token(self, token: Token) -> None: ...

    def load_refresh_token(self) -> str | None: ...

    def save_refresh_token(self, refresh_token: str) -> None: ...
```

In `SqliteStore.__init__`, call a migration immediately after `self._conn.executescript(SCHEMA)`, inside the same `try`:

```python
            self._conn.executescript(SCHEMA)
            self._migrate()
```

Add the migration and the two accessors to `SqliteStore`:

```python
    def _migrate(self) -> None:
        """Add columns that post-date the original schema.

        SCHEMA uses CREATE TABLE IF NOT EXISTS, so a database created by an
        earlier version keeps its original columns forever. The column is
        nullable, so there is nothing to backfill.
        """
        columns = {
            str(row["name"]) for row in self._conn.execute("PRAGMA table_info(token_cache)")
        }
        if "refresh_token" not in columns:
            self._conn.execute("ALTER TABLE token_cache ADD COLUMN refresh_token TEXT")

    def load_refresh_token(self) -> str | None:
        with _store_errors("load refresh token"):
            row = self._conn.execute(
                "SELECT refresh_token FROM token_cache WHERE id = 1"
            ).fetchone()
            if row is None or row["refresh_token"] is None:
                return None
            return str(row["refresh_token"])

    def save_refresh_token(self, refresh_token: str) -> None:
        """Store the refresh token, creating the row if no access token exists.

        The placeholder access token is deliberately already expired, so a
        later load_token() reports it unusable and triggers a refresh rather
        than sending an empty bearer token.
        """
        with _store_errors("save refresh token"):
            self._conn.execute(
                """
                INSERT INTO token_cache (id, access_token, expires_at, refresh_token)
                VALUES (1, '', '1970-01-01T00:00:00+00:00', ?)
                ON CONFLICT(id) DO UPDATE SET
                    refresh_token = excluded.refresh_token
                """,
                (refresh_token,),
            )
```

Leave `save_token` exactly as it is — its `ON CONFLICT` clause already updates only `access_token` and `expires_at`, which is what makes the third test pass.

Mirror both methods on `InMemoryStore`, adding `self._refresh_token: str | None = None` to its `__init__`:

```python
    def load_refresh_token(self) -> str | None:
        return self._refresh_token

    def save_refresh_token(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd script && uv run pytest tests/test_store.py -v && uv run mypy`
Expected: PASS. mypy clean — note that `SpyCache(TokenCache)` in `tests/test_auth.py` now needs the two new methods; add them there returning `None` / doing nothing if mypy flags it.

- [ ] **Step 5: Commit**

```bash
git add script/src/dawatch/store.py script/tests/test_store.py script/tests/test_auth.py
git commit -m "feat: persist a refresh token in the token cache

Additive column plus a PRAGMA-guarded ALTER, so an existing PVC migrates on
open rather than failing every query."
```

---

### Task 3: Add the refresh token to configuration

**Files:**
- Modify: `script/src/dawatch/config.py` (add field)
- Modify: `script/.env.example`
- Test: `script/tests/test_config.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `Settings.refresh_token: SecretStr | None`, read from `DAWATCH_REFRESH_TOKEN`. Task 6 and Task 7 read it.

- [ ] **Step 1: Write the failing test**

Add to `script/tests/test_config.py`:

```python
def test_reads_the_refresh_token_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVIANTART_CLIENT_ID", "id")
    monkeypatch.setenv("DEVIANTART_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DAWATCH_NTFY_TOPIC", "topic")
    monkeypatch.setenv("DAWATCH_REFRESH_TOKEN", "refresh-abc")

    settings = Settings.load()

    assert settings.refresh_token is not None
    assert settings.refresh_token.get_secret_value() == "refresh-abc"


def test_refresh_token_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent is legal: the store may already hold a rotated token.

    It is also legal for `dawatch login`, which runs before any token exists.
    """
    monkeypatch.setenv("DEVIANTART_CLIENT_ID", "id")
    monkeypatch.setenv("DEVIANTART_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DAWATCH_NTFY_TOPIC", "topic")

    assert Settings.load().refresh_token is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd script && uv run pytest tests/test_config.py -k refresh -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'refresh_token'`.

- [ ] **Step 3: Implement**

In `script/src/dawatch/config.py`, add the field directly below `client_secret`:

```python
    # Optional: the store may already hold a rotated token, and `dawatch login`
    # runs before any token exists at all.
    refresh_token: SecretStr | None = None
```

The `DAWATCH_` env prefix maps this to `DAWATCH_REFRESH_TOKEN` automatically — no `validation_alias` needed, unlike the two `DEVIANTART_`-prefixed credentials.

Add to `script/.env.example`, under the credentials block:

```
# OAuth2 refresh token for the watched-artists feed. Obtain with `dawatch login`.
# Expires after 3 months, after which you must re-run `dawatch login`.
DAWATCH_REFRESH_TOKEN=
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd script && uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add script/src/dawatch/config.py script/tests/test_config.py script/.env.example
git commit -m "feat: add DAWATCH_REFRESH_TOKEN to settings"
```

---

### Task 4: RefreshTokenAuth

**Files:**
- Modify: `script/src/dawatch/auth.py` (extract shared caching into a base, add the new class)
- Test: `script/tests/test_auth.py`

**Interfaces:**
- Consumes: `TokenCache.load_refresh_token()` / `save_refresh_token()` from Task 2.
- Produces: `RefreshTokenAuth(http: httpx.Client, client_id: str, client_secret: str, cache: TokenCache, seed_refresh_token: str | None = None)`, satisfying the existing `TokenProvider` Protocol (`token() -> str`, `invalidate() -> None`). Task 7 constructs it.

- [ ] **Step 1: Write the failing tests**

Add to `script/tests/test_auth.py`:

```python
REFRESH_RESPONSE = {
    "status": "success",
    "access_token": "fresh-token",
    "token_type": "Bearer",
    "expires_in": 3600,
    "refresh_token": "rotated-refresh",
}


@pytest.fixture
def refresh_auth(cache: InMemoryStore) -> Any:
    def build(seed: str | None = "seed-refresh") -> RefreshTokenAuth:
        return RefreshTokenAuth(
            httpx.Client(),
            client_id="id",
            client_secret="secret",
            cache=cache,
            seed_refresh_token=seed,
        )

    return build


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_sends_the_refresh_token_grant(refresh_auth: Any) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=REFRESH_RESPONSE))

    assert refresh_auth().token() == "fresh-token"

    body = route.calls[0].request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=seed-refresh" in body


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_prefers_a_stored_refresh_token_over_the_seed(
    refresh_auth: Any, cache: InMemoryStore
) -> None:
    """The store holds the rotated token; the seed is only a bootstrap."""
    cache.save_refresh_token("stored-refresh")
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=REFRESH_RESPONSE))

    refresh_auth().token()

    assert "refresh_token=stored-refresh" in route.calls[0].request.content.decode()


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_persists_a_rotated_refresh_token(refresh_auth: Any, cache: InMemoryStore) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=REFRESH_RESPONSE))

    refresh_auth().token()

    assert cache.load_refresh_token() == "rotated-refresh"


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_keeps_the_current_token_when_none_is_returned(
    refresh_auth: Any, cache: InMemoryStore
) -> None:
    """DeviantArt does not document whether refresh tokens rotate.

    A response without a refresh_token must leave the existing one intact
    rather than clearing it.
    """
    cache.save_refresh_token("stored-refresh")
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    refresh_auth().token()

    assert cache.load_refresh_token() == "stored-refresh"


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_expired_refresh_token_raises_config_error(refresh_auth: Any) -> None:
    """invalid_grant can never succeed on retry, so it is a config failure.

    Exit 1 would let backoffLimit retry a token that is dead for three months.
    """
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )

    with pytest.raises(ConfigError, match="dawatch login"):
        refresh_auth().token()


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_other_rejections_raise_auth_error(refresh_auth: Any) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="upstream down"))

    with pytest.raises(AuthError):
        refresh_auth().token()


@time_machine.travel(NOW, tick=False)
def test_missing_refresh_token_raises_config_error(refresh_auth: Any) -> None:
    with pytest.raises(ConfigError, match="dawatch login"):
        refresh_auth(seed=None).token()


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_reuses_a_cached_access_token(refresh_auth: Any, cache: InMemoryStore) -> None:
    """Caching behaviour is inherited and must not regress."""
    cache.save_token(Token(access_token="cached", expires_at=NOW + timedelta(hours=1)))
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=REFRESH_RESPONSE))

    assert refresh_auth().token() == "cached"
    assert route.call_count == 0
```

Add `RefreshTokenAuth` to the `dawatch.auth` import line and `ConfigError` to the `dawatch.errors` import line.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd script && uv run pytest tests/test_auth.py -k refresh -v`
Expected: FAIL with `ImportError: cannot import name 'RefreshTokenAuth'`.

- [ ] **Step 3: Implement**

`DeviantArtAuth.token()` and `invalidate()` are grant-agnostic — only `_request_token` differs. Extract the shared half into a base class rather than duplicating it. In `script/src/dawatch/auth.py`:

```python
from dawatch.errors import AuthError, ConfigError


class _CachedTokenAuth:
    """Access-token caching shared by every grant.

    Subclasses supply only _request_token. The caching rules — in-process
    memory, then the persistent cache, then the network — are identical
    regardless of how the token is obtained.
    """

    def __init__(self, cache: TokenCache) -> None:
        self._cache = cache
        self._current: Token | None = None
        self._force_refresh: bool = False

    def token(self) -> str:
        """Return a usable access token, fetching a new one only if needed."""
        now = datetime.now(UTC)

        if not self._force_refresh:
            if self._current is not None and self._current.is_valid(now):
                return self._current.access_token

            cached = self._cache.load_token()
            if cached is not None and cached.is_valid(now):
                log.debug("token.cache_hit", expires_at=cached.expires_at.isoformat())
                self._current = cached
                return cached.access_token

        fresh = self._request_token(now)
        self._current = fresh
        self._cache.save_token(fresh)
        self._force_refresh = False
        log.info("token.refreshed", expires_at=fresh.expires_at.isoformat())
        return fresh.access_token

    def invalidate(self) -> None:
        """Force the next call to re-authenticate, skipping all caches."""
        self._current = None
        self._force_refresh = True

    def _request_token(self, now: datetime) -> Token:
        raise NotImplementedError
```

Rewrite `DeviantArtAuth` to inherit it, keeping its existing `_request_token` body verbatim:

```python
class DeviantArtAuth(_CachedTokenAuth):
    """Supplies a bearer token for the application itself."""

    def __init__(
        self,
        http: httpx.Client,
        client_id: str,
        client_secret: str,
        cache: TokenCache,
    ) -> None:
        super().__init__(cache)
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret

    # _request_token stays exactly as it is today.
```

Add the new class:

```python
class RefreshTokenAuth(_CachedTokenAuth):
    """Supplies a bearer token that acts as the user, not the application.

    Only this grant can see who the user watches. The refresh token is read
    from the store first and the configured seed second, so a rotated token
    always wins over the value baked into the environment.
    """

    def __init__(
        self,
        http: httpx.Client,
        client_id: str,
        client_secret: str,
        cache: TokenCache,
        seed_refresh_token: str | None = None,
    ) -> None:
        super().__init__(cache)
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._seed = seed_refresh_token

    def _request_token(self, now: datetime) -> Token:
        refresh_token = self._cache.load_refresh_token() or self._seed
        if not refresh_token:
            raise ConfigError(
                "No refresh token available. Run 'dawatch login' and store the "
                "result as DAWATCH_REFRESH_TOKEN."
            )

        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                },
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"Could not reach the token endpoint: {type(exc).__name__}") from exc

        if response.status_code != httpx.codes.OK:
            self._raise_for_rejection(response)

        try:
            payload: dict[str, Any] = response.json()
            token = Token.from_response(payload, now)
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthError("Token endpoint returned a malformed response") from exc

        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != refresh_token:
            self._cache.save_refresh_token(rotated)
            log.info("token.refresh_token_rotated")

        return token

    @staticmethod
    def _raise_for_rejection(response: httpx.Response) -> None:
        """Separate a dead refresh token from every other rejection.

        A refresh token expires after three months and cannot be revived by
        retrying, so it must surface as a configuration failure. Only the
        'error' field is read: the rest of the body can echo credentials.
        """
        error_code = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error_code = str(body.get("error", ""))
        except ValueError:
            pass

        if error_code == "invalid_grant":
            raise ConfigError(
                "The refresh token has expired or been revoked. Run 'dawatch login' "
                "and store the new DAWATCH_REFRESH_TOKEN."
            )

        raise AuthError(
            f"Token endpoint rejected the refresh grant (HTTP {response.status_code}). "
            f"Check DEVIANTART_CLIENT_ID and DEVIANTART_CLIENT_SECRET."
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd script && uv run pytest tests/test_auth.py -v && uv run mypy`
Expected: PASS. Every pre-existing `DeviantArtAuth` test must still pass — the refactor is behaviour-preserving.

- [ ] **Step 5: Commit**

```bash
git add script/src/dawatch/auth.py script/tests/test_auth.py
git commit -m "feat: add RefreshTokenAuth for the authorization_code grant

Shared access-token caching moves to a base class so both grants stay
identical where they should be. invalid_grant maps to ConfigError, since a
token dead for three months can never succeed on retry."
```

---

### Task 5: WatchedDeviationsSource

**Files:**
- Modify: `script/src/dawatch/sources.py` (add class, update module docstring)
- Test: `script/tests/test_sources.py`

**Interfaces:**
- Consumes: the existing `JsonClient` Protocol.
- Produces: `WatchedDeviationsSource(client: JsonClient, seen: Callable[[str], bool], page_size: int = 50, max_pages: int = 5)` with `fetch(date: str | None = None) -> list[Deviation]`, satisfying `DeviationSource`. Task 7 constructs it.

- [ ] **Step 1: Write the failing tests**

Add to `script/tests/test_sources.py`:

```python
class PagingStubClient:
    """Returns a scripted sequence of pages and records the params it saw."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, str] | None] = []

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append(params)
        return self.pages[len(self.calls) - 1]


def _page(ids: list[str], has_more: bool, next_offset: int | None = None) -> dict[str, Any]:
    return {
        "has_more": has_more,
        "next_offset": next_offset,
        "results": [
            {"deviationid": i, "title": f"Art {i}", "author": {"username": "alice"}} for i in ids
        ],
    }


def test_requests_the_watch_feed_path() -> None:
    client = PagingStubClient([_page(["A"], has_more=False)])
    source = WatchedDeviationsSource(client, seen=lambda _: False)

    source.fetch()

    assert client.calls[0] == {"offset": "0", "limit": "50"}


def test_stops_at_the_first_already_seen_deviation() -> None:
    """The stop rule is what makes a quiet run cost one request."""
    client = PagingStubClient([_page(["NEW1", "NEW2", "OLD", "OLDER"], has_more=True)])
    source = WatchedDeviationsSource(client, seen=lambda d: d.startswith("OLD"))

    result = source.fetch()

    assert [d.deviationid for d in result] == ["NEW1", "NEW2"]
    assert len(client.calls) == 1


def test_pages_forward_while_everything_is_new() -> None:
    client = PagingStubClient(
        [
            _page(["A", "B"], has_more=True, next_offset=50),
            _page(["C", "SEEN"], has_more=True, next_offset=100),
        ]
    )
    source = WatchedDeviationsSource(client, seen=lambda d: d == "SEEN")

    result = source.fetch()

    assert [d.deviationid for d in result] == ["A", "B", "C"]
    assert client.calls[1] == {"offset": "50", "limit": "50"}


def test_stops_when_the_api_reports_no_more_pages() -> None:
    client = PagingStubClient([_page(["A"], has_more=False)])
    source = WatchedDeviationsSource(client, seen=lambda _: False)

    assert len(source.fetch()) == 1
    assert len(client.calls) == 1


def test_respects_the_page_cap() -> None:
    """A first run against a large watch list must terminate."""
    client = PagingStubClient([_page([f"P{n}"], has_more=True, next_offset=n + 1) for n in range(10)])
    source = WatchedDeviationsSource(client, seen=lambda _: False, max_pages=3)

    result = source.fetch()

    assert len(client.calls) == 3
    assert len(result) == 3


def test_falls_back_to_a_computed_offset_when_next_offset_is_absent() -> None:
    client = PagingStubClient(
        [_page(["A", "B"], has_more=True), _page(["C"], has_more=False)]
    )
    source = WatchedDeviationsSource(client, seen=lambda _: False)

    source.fetch()

    assert client.calls[1] == {"offset": "2", "limit": "50"}


def test_rejects_a_date_argument() -> None:
    """The watch feed has no date parameter, so --date must fail loudly."""
    client = PagingStubClient([_page(["A"], has_more=False)])
    source = WatchedDeviationsSource(client, seen=lambda _: False)

    with pytest.raises(FetchError, match="date"):
        source.fetch("2026-08-01")


def test_skips_a_malformed_result_without_losing_the_page() -> None:
    client = PagingStubClient(
        [
            {
                "has_more": False,
                "results": [
                    {"deviationid": "A", "title": "Good", "author": {"username": "alice"}},
                    {"title": "No id at all"},
                    {"deviationid": "C", "title": "Also good", "author": {"username": "bob"}},
                ],
            }
        ]
    )
    source = WatchedDeviationsSource(client, seen=lambda _: False)

    assert [d.deviationid for d in source.fetch()] == ["A", "C"]


def test_unparseable_payload_raises_fetch_error() -> None:
    client = PagingStubClient([{"results": "not a list"}])
    source = WatchedDeviationsSource(client, seen=lambda _: False)

    with pytest.raises(FetchError):
        source.fetch()
```

Add `WatchedDeviationsSource` to the `dawatch.sources` import line.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd script && uv run pytest tests/test_sources.py -k "watch or paging or page or seen or date_argument" -v`
Expected: FAIL with `ImportError: cannot import name 'WatchedDeviationsSource'`.

- [ ] **Step 3: Implement**

Update the module docstring at the top of `script/src/dawatch/sources.py`:

```python
"""Sources of deviations.

Two feeds, one Protocol. Daily Deviations is a single dated request with no
pagination; the watch feed is an offset-paged stream with no date at all. The
orchestration layer sees neither difference.
"""
```

Add `from collections.abc import Callable` to the imports, then the class:

```python
class WatchedDeviationsSource:
    """Deviations posted by the artists the authenticated user watches.

    Requires a user-scoped token: the client_credentials grant authenticates
    the application, which watches nobody.

    Paging stops at the first deviation already in the store, which makes a
    quiet run cost exactly one request while still catching up after an
    outage. MAX_PAGES bounds the first run against a large watch list.
    """

    PATH = "browse/deviantsyouwatch"
    PAGE_SIZE = 50
    MAX_PAGES = 5

    def __init__(
        self,
        client: JsonClient,
        seen: Callable[[str], bool],
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._client = client
        self._seen = seen
        self._page_size = page_size
        self._max_pages = max_pages

    def fetch(self, date: str | None = None) -> list[Deviation]:
        """Return everything posted since the last already-seen deviation.

        Raises:
            FetchError: if ``date`` is supplied, or a page cannot be
                interpreted as a feed at all.
        """
        if date is not None:
            raise FetchError(
                "The watched-artists feed has no date parameter. "
                "Remove --date, or use the Daily Deviations source."
            )

        collected: list[Deviation] = []
        offset = 0
        pages_read = 0

        while pages_read < self._max_pages:
            payload = self._client.get_json(
                self.PATH, {"offset": str(offset), "limit": str(self._page_size)}
            )
            pages_read += 1

            raw_results = payload.get("results", [])
            if not isinstance(raw_results, list):
                raise FetchError(
                    f"Feed payload had {type(raw_results).__name__} results, expected a list"
                )

            reached_seen = False
            for raw in raw_results:
                deviation = self._parse(raw)
                if deviation is None:
                    continue
                if self._seen(deviation.deviationid):
                    reached_seen = True
                    break
                collected.append(deviation)

            if reached_seen or not payload.get("has_more"):
                break

            next_offset = payload.get("next_offset")
            offset = int(next_offset) if next_offset is not None else offset + len(raw_results)

        log.info(
            "source.fetched",
            count=len(collected),
            pages=pages_read,
            feed="deviantsyouwatch",
        )
        return collected

    @staticmethod
    def _parse(raw: Any) -> Deviation | None:
        """Return a Deviation, or None if this single row is unusable."""
        try:
            return Deviation.model_validate(raw)
        except ValidationError as exc:
            # One unusable row should not cost the user every other
            # notification in the batch.
            log.warning(
                "source.skipped_malformed_result",
                error=str(exc),
                raw_id=raw.get("deviationid") if isinstance(raw, dict) else None,
            )
            return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd script && uv run pytest tests/test_sources.py -v && uv run mypy`
Expected: PASS, including every pre-existing `DailyDeviationsSource` test.

- [ ] **Step 5: Commit**

```bash
git add script/src/dawatch/sources.py script/tests/test_sources.py
git commit -m "feat: add WatchedDeviationsSource

Pages until it meets an already-seen deviation, capped at five pages. A quiet
run costs one request; a run after an outage catches up without needing a date."
```

---

### Task 6: The `dawatch login` command

**Files:**
- Create: `script/src/dawatch/login.py`
- Create: `script/tests/test_login.py`
- Modify: `script/src/dawatch/cli.py` (register the subcommand)

**Interfaces:**
- Consumes: `Settings` from Task 3.
- Produces: `dawatch.login.build_authorize_url(client_id: str, state: str, redirect_uri: str = REDIRECT_URI, scope: str = SCOPE) -> str` and `dawatch.login.exchange_code(http: httpx.Client, client_id: str, client_secret: str, code: str, redirect_uri: str = REDIRECT_URI) -> str` returning the refresh token. Task 7 wires the subcommand into `main`.

Keeping this in its own module keeps a browser flow, a socket listener, and a `while` loop out of `cli.py`, which is otherwise pure dispatch.

- [ ] **Step 1: Write the failing tests**

Create `script/tests/test_login.py`:

```python
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from dawatch.auth import TOKEN_URL
from dawatch.errors import AuthError, ConfigError
from dawatch.login import REDIRECT_URI, SCOPE, build_authorize_url, exchange_code


def test_authorize_url_carries_the_required_parameters() -> None:
    url = build_authorize_url(client_id="12345", state="abc123")

    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["12345"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["scope"] == [SCOPE]
    assert query["state"] == ["abc123"]


@respx.mock
def test_exchange_code_returns_the_refresh_token() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "access_token": "access-abc",
                "refresh_token": "refresh-xyz",
                "expires_in": 3600,
            },
        )
    )

    with httpx.Client() as http:
        assert exchange_code(http, "id", "secret", "the-code") == "refresh-xyz"


@respx.mock
def test_exchange_code_sends_the_authorization_code_grant() -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        )
    )

    with httpx.Client() as http:
        exchange_code(http, "id", "secret", "the-code")

    body = route.calls[0].request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "code=the-code" in body


@respx.mock
def test_exchange_code_without_a_refresh_token_is_a_config_error() -> None:
    """A response with no refresh_token usually means the scope was refused."""
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    )

    with httpx.Client() as http, pytest.raises(ConfigError, match="refresh token"):
        exchange_code(http, "id", "secret", "the-code")


@respx.mock
def test_rejected_code_raises_auth_error() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    with httpx.Client() as http, pytest.raises(AuthError):
        exchange_code(http, "id", "secret", "the-code")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd script && uv run pytest tests/test_login.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.login'`.

- [ ] **Step 3: Implement**

Create `script/src/dawatch/login.py`:

```python
"""One-time interactive consent for the authorization_code grant.

A CronJob cannot open a browser, so the refresh token is minted here, on a
workstation, and handed to the operator to place in OpenBao. This module runs
once every three months and never inside the cluster.
"""

import http.server
import secrets
import threading
import urllib.parse
import webbrowser
from typing import Any

import httpx
import structlog

from dawatch.auth import TOKEN_URL
from dawatch.errors import AuthError, ConfigError

AUTHORIZE_URL = "https://www.deviantart.com/oauth2/authorize"
REDIRECT_URI = "http://localhost:8080/callback"
# The watch feed lives behind 'browse'. If DeviantArt refuses it, consent fails
# visibly here rather than as an empty feed in a scheduled run.
SCOPE = "browse"
CALLBACK_TIMEOUT_SECONDS = 300

log = structlog.get_logger(__name__)


def build_authorize_url(
    client_id: str,
    state: str,
    redirect_uri: str = REDIRECT_URI,
    scope: str = SCOPE,
) -> str:
    """Return the URL the user must visit to grant consent."""
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(
    http: httpx.Client,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = REDIRECT_URI,
) -> str:
    """Trade an authorization code for a refresh token.

    Raises:
        AuthError: if the token endpoint refuses the code.
        ConfigError: if the exchange succeeds but carries no refresh token,
            which means the granted scope was not what was asked for.
    """
    try:
        response = http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach the token endpoint: {type(exc).__name__}") from exc

    if response.status_code != httpx.codes.OK:
        # The body is deliberately not included: it can echo credentials.
        raise AuthError(
            f"Token endpoint rejected the authorization code (HTTP {response.status_code})."
        )

    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise AuthError("Token endpoint returned a malformed response") from exc

    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConfigError(
            "The authorization succeeded but returned no refresh token. "
            f"Confirm the app is registered with the '{SCOPE}' scope."
        )

    return refresh_token


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the single redirect DeviantArt sends back."""

    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = query.get("code", [None])[0]
        _CallbackHandler.state = query.get("state", [None])[0]
        _CallbackHandler.error = query.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"dawatch: authorization received. You can close this tab.")

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log."""


def run_consent_flow(http_client: httpx.Client, client_id: str, client_secret: str) -> str:
    """Open a browser, capture the redirect, and return the refresh token.

    Raises:
        ConfigError: if consent is denied, times out, or the returned state
            does not match the one sent.
    """
    expected_state = secrets.token_urlsafe(16)
    _CallbackHandler.code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None

    server = http.server.HTTPServer(("localhost", 8080), _CallbackHandler)
    server.timeout = CALLBACK_TIMEOUT_SECONDS

    url = build_authorize_url(client_id, expected_state)
    print(f"Opening your browser to authorize dawatch.\nIf it does not open: {url}\n")
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        server.handle_request()
    finally:
        server.server_close()

    if _CallbackHandler.error:
        raise ConfigError(f"Authorization was refused: {_CallbackHandler.error}")
    if _CallbackHandler.code is None:
        raise ConfigError(
            f"No authorization code arrived within {CALLBACK_TIMEOUT_SECONDS} seconds."
        )
    if _CallbackHandler.state != expected_state:
        # A mismatched state means the response did not originate from the
        # request this process started.
        raise ConfigError("Authorization state did not match. Start over.")

    return exchange_code(http_client, client_id, client_secret, _CallbackHandler.code)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd script && uv run pytest tests/test_login.py -v && uv run mypy`
Expected: PASS. `run_consent_flow` is not unit-tested — it binds a socket and opens a browser. Its two testable halves are covered directly.

- [ ] **Step 5: Commit**

```bash
git add script/src/dawatch/login.py script/tests/test_login.py
git commit -m "feat: add the one-time consent flow

A CronJob cannot open a browser, so the refresh token is minted on a
workstation and handed to the operator to place in OpenBao."
```

---

### Task 7: Wire the watch feed into the CLI

**Files:**
- Modify: `script/src/dawatch/cli.py` (imports, parser, `main`, `_doctor`)
- Test: `script/tests/test_cli.py`

**Interfaces:**
- Consumes: `RefreshTokenAuth` (Task 4), `WatchedDeviationsSource` (Task 5), `run_consent_flow` (Task 6), `Settings.refresh_token` (Task 3).
- Produces: the finished command surface. Nothing depends on it.

- [ ] **Step 1: Write the failing tests**

Add to `script/tests/test_cli.py`:

```python
def test_run_uses_the_watch_feed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The run path must construct the watch source, not Daily Deviations."""
    _set_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DAWATCH_REFRESH_TOKEN", "refresh-abc")

    constructed: list[str] = []
    monkeypatch.setattr(
        "dawatch.cli.WatchedDeviationsSource",
        lambda *args, **kwargs: constructed.append("watch") or _EmptySource(),
    )

    main(["run", "--dry-run"])

    assert constructed == ["watch"]


def test_run_rejects_a_date(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--date is meaningless against a feed with no date parameter."""
    _set_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DAWATCH_REFRESH_TOKEN", "refresh-abc")

    assert main(["run", "--date", "2026-08-01"]) == EXIT_CONFIG


def test_expired_refresh_token_exits_config_not_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exit 2 stops backoffLimit retrying a token dead for three months."""
    _set_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DAWATCH_REFRESH_TOKEN", "expired")

    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        assert main(["run"]) == EXIT_CONFIG


def test_transient_failure_still_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DAWATCH_REFRESH_TOKEN", "refresh-abc")

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
        assert main(["run"]) == EXIT_FAILURE
```

Add a minimal source stub near the top of the file:

```python
class _EmptySource:
    def fetch(self, date: str | None = None) -> list[Deviation]:
        return []
```

If `test_cli.py` has no `_set_required_env` helper, add one that sets `DEVIANTART_CLIENT_ID`, `DEVIANTART_CLIENT_SECRET`, `DAWATCH_NTFY_TOPIC`, and `DAWATCH_DB_PATH` to a path under `tmp_path`, matching whatever pattern the existing tests in that file already use.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd script && uv run pytest tests/test_cli.py -k "watch_feed or rejects_a_date or expired_refresh" -v`
Expected: FAIL — `dawatch.cli` has no attribute `WatchedDeviationsSource`, and `--date` currently returns `EXIT_OK`.

- [ ] **Step 3: Implement**

In `script/src/dawatch/cli.py`, update the imports:

```python
from dawatch.auth import DeviantArtAuth, RefreshTokenAuth, TokenProvider
from dawatch.login import run_consent_flow
from dawatch.sources import DailyDeviationsSource, WatchedDeviationsSource
```

Register the subcommand in `build_parser`, and correct the program description, which still names Daily Deviations:

```python
    parser = argparse.ArgumentParser(
        prog="dawatch",
        description="Push new deviations from the artists you watch to your phone.",
    )
```

```python
    subparsers.add_parser(
        "login", help="Authorize dawatch against your account and print a refresh token"
    )
```

In `main`, handle `login` before the store is opened — it needs no database:

```python
            if args.command == "login":
                refresh_token = run_consent_flow(
                    http,
                    settings.client_id.get_secret_value(),
                    settings.client_secret.get_secret_value(),
                )
                print("\nRefresh token (store this in OpenBao as DAWATCH_REFRESH_TOKEN):")
                print(refresh_token)
                print("\nIt expires in 3 months. Re-run 'dawatch login' when it does.")
                return EXIT_OK
```

Replace the auth and source construction:

```python
            auth: TokenProvider = RefreshTokenAuth(
                http,
                settings.client_id.get_secret_value(),
                settings.client_secret.get_secret_value(),
                store,
                seed_refresh_token=(
                    settings.refresh_token.get_secret_value()
                    if settings.refresh_token is not None
                    else None
                ),
            )

            if args.command == "doctor":
                return _doctor(settings, http, auth)

            client = DeviantArtClient(http, auth, max_retries=settings.max_retries)
            source = WatchedDeviationsSource(client, seen=store.has_seen)
```

Widen `_doctor`'s signature — it calls only `auth.token()`, so the Protocol is sufficient and the health check now covers whichever grant is deployed:

```python
def _doctor(settings: Settings, http: httpx.Client, auth: TokenProvider) -> int:
```

Then make `doctor` prove the granted scope. `/placebo` answers "is this token live" but not "may it read the watch feed" — and a token with the wrong scope fails only at 09:00 the next morning otherwise. Replace the existing `api` probe's path with the real endpoint, asking for a single item:

```python
    try:
        response = http.get(
            f"{API_BASE}/browse/deviantsyouwatch",
            params={"limit": "1"},
            headers={"Authorization": f"Bearer {token}", "dA-minor-version": API_VERSION},
        )
        if response.status_code == httpx.codes.FORBIDDEN:
            report(
                "api",
                False,
                "token lacks the scope for the watch feed; re-run 'dawatch login'",
            )
        else:
            report("api", response.status_code == httpx.codes.OK, f"HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        report("api", False, type(exc).__name__)
```

Add the matching test to `script/tests/test_cli.py`:

```python
def test_doctor_reports_an_insufficient_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wrong scope must fail here, not silently at 09:00 tomorrow."""
    _set_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DAWATCH_REFRESH_TOKEN", "refresh-abc")

    with respx.mock:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "a", "expires_in": 3600, "refresh_token": "r"}
            )
        )
        respx.get(url__startswith=f"{API_BASE}/browse/deviantsyouwatch").mock(
            return_value=httpx.Response(403)
        )
        assert main(["doctor"]) != EXIT_OK

    assert "scope" in capsys.readouterr().out
```

Route configuration failures to exit 2. `ConfigError` subclasses `DawatchError`, so its handler must come first:

```python
    except ConfigError as exc:
        # Never succeeds on retry: an expired refresh token, or a missing one.
        # Exit 2 tells the CronJob's backoffLimit not to bother.
        log.error("run.config_failed", error=str(exc), error_type=type(exc).__name__)
        return EXIT_CONFIG
    except DawatchError as exc:
        log.error("run.failed", error=str(exc), error_type=type(exc).__name__)
        return EXIT_FAILURE
```

`DailyDeviationsSource` stays imported and available; nothing in `main` constructs it any more.

- [ ] **Step 4: Run the full suite**

Run: `cd script && uv run pytest -v && uv run mypy && uv run ruff check . && uv run ruff format --check .`
Expected: PASS, coverage at or above 90%. Fix any `test_cli.py` test that assumed `--date` was accepted or that Daily Deviations was the run source — those assumptions are now deliberately wrong.

- [ ] **Step 5: Commit**

```bash
git add script/src/dawatch/cli.py script/tests/test_cli.py
git commit -m "feat: run against the watched-artists feed

Adds 'dawatch login', drops Daily Deviations from the run path, and maps
configuration failures to exit 2 so backoffLimit stops retrying them."
```

---

### Task 8: Deployment and documentation

**Files:**
- Modify: `k8s/overlays/prod/secret.yaml` (add the refresh token key)
- Modify: `k8s/overlays/prod/patch-cronjob.yaml` (restore a sane schedule)
- Modify: `README.md`

**Interfaces:**
- Consumes: `DAWATCH_REFRESH_TOKEN` from Task 3.
- Produces: nothing code depends on.

- [ ] **Step 1: Add the refresh token to the ExternalSecret**

In `k8s/overlays/prod/secret.yaml`, add a third entry under `data`:

```yaml
    - secretKey: DAWATCH_REFRESH_TOKEN
      remoteRef:
        key: deviantart/config
        property: DAWATCH_REFRESH_TOKEN
```

- [ ] **Step 2: Restore the schedule**

`k8s/overlays/prod/patch-cronjob.yaml` currently reads `*/5 * * * *`, which was set for testing. A watch feed polled every five minutes is almost entirely empty requests. Set it to hourly:

```yaml
  schedule: "0 * * * *"
```

- [ ] **Step 3: Mint and store the refresh token**

This step is the operator's, not the agent's. It cannot be automated: it requires a browser and a human.

```bash
cd script
uv run dawatch login          # opens a browser, prints a refresh token

kubectl -n open-bao exec openbao-0 -- env \
  BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="<root-token>" \
  bao kv patch secret/deviantart/config DAWATCH_REFRESH_TOKEN="<printed-token>"

kubectl -n dawatch annotate externalsecret secret force-sync=$(date +%s) --overwrite
```

Prerequisite: `http://localhost:8080/callback` must be registered as a redirect URI on the DeviantArt application at https://www.deviantart.com/developers/apps, or consent fails before any token exists.

- [ ] **Step 4: Update the README**

- Replace the "Daily Deviations" framing in the opening description and the `How it works` diagram with the watch feed.
- Delete the "Not implemented" paragraph about the authorization code flow — it is now implemented.
- Add `dawatch login` to the Commands table.
- Add `DAWATCH_REFRESH_TOKEN` to the Configuration table, noting the 3-month expiry.
- Add to Operational notes: the refresh token expires quarterly and surfaces as the staleness alert; the fix is to re-run `dawatch login`.

- [ ] **Step 5: Verify against the cluster**

```bash
cd script && uv run dawatch doctor      # locally, with .env populated
make image && kind load docker-image dawatch:0.1.0 --name dawatch
kubectl apply -k k8s/overlays/prod
kubectl -n dawatch create job dawatch-verify --from=cronjob/dawatch
kubectl -n dawatch logs -f job/dawatch-verify
```

Expected in the logs: `token.refreshed`, then `source.fetched` with `feed=deviantsyouwatch`, then `run.complete` with `errors: 0`.

Note that the store already holds the Daily Deviations marked seen on 2026-08-29. Those IDs will not collide with watch-feed IDs, so the first watch run sees an already-populated store and will **not** seed — it notifies everything the paging rule collects, up to 250 deviations. Run it once with `--dry-run` first and check the count before letting it notify.

- [ ] **Step 6: Commit**

```bash
git add k8s/overlays/prod/secret.yaml k8s/overlays/prod/patch-cronjob.yaml README.md
git commit -m "feat: deploy the watched-artists feed

Adds the refresh token to the ExternalSecret and returns the schedule to
hourly, which suits a watch feed better than the five-minute test cadence."
```

---

## Notes for the executor

**The scope string is unconfirmed.** `SCOPE = "browse"` in `login.py` is the design's best guess; DeviantArt's reference docs 404'd during design. Task 6 fails visibly if it is wrong — consent either errors, or returns a token with no `refresh_token`, which `exchange_code` already raises `ConfigError` for. If it is wrong, the fix is one constant, and `browse.deviantsyouwatch` is the next candidate to try.

**Task 1 is independently shippable.** It fixes a bug that is live in the cluster right now. If the rest of this plan stalls, that commit should still land.

**Task order matters for tasks 2 → 4 → 7.** Task 4 needs the store methods; Task 7 needs both the auth and the source. Tasks 1, 3, 5, and 6 are independent of each other and can be done in any order.

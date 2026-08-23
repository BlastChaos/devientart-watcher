# DeviantArt Daily Deviation Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll the DeviantArt Daily Deviations feed on a schedule, detect deviations not seen on a previous run, and push each one to the user's phone via ntfy.

**Architecture:** A one-shot CLI (`dawatch run`) performs a single poll and exits; Kubernetes owns the schedule. Four protocols — `DeviationSource`, `SeenStore`, `Notifier`, `MetricsSink` — form the seams, so `WatchService` orchestrates without touching a network, a clock, or a filesystem in tests. State lives in SQLite on a PersistentVolumeClaim, because a CronJob pod's filesystem does not outlive the run.

**Tech Stack:** Python 3.13+, httpx, pydantic v2, pydantic-settings, structlog, prometheus-client, pytest + respx + time-machine, ruff, mypy, uv, Docker, kind, Kustomize, Prometheus Pushgateway, Grafana.

**Spec:** `docs/superpowers/specs/2026-08-23-deviantart-watcher-design.md`

## Global Constraints

- Python 3.13 and 3.14 must both pass CI. Do not use syntax newer than 3.13.
- `mypy --strict` must pass with zero errors. Every function is annotated.
- `ruff check` and `ruff format --check` must pass.
- All source lives under `script/src/dawatch/`; all tests under `script/tests/`.
- All Kubernetes manifests live under `k8s/` at the repo root, never inside `script/`.
- Secrets are typed `SecretStr` and never interpolated into log messages or exception text.
- No network access in any unit test. `respx` intercepts httpx; there is no live-API test.
- The DeviantArt API version header `dA-minor-version: 20240701` is sent on every API request.
- The endpoint `browse/dailydeviations` accepts **only** a `date` parameter. It has no `limit` and no `offset`. Never write pagination code against it.
- The `client_credentials` grant issues **no refresh token**. Expiry is handled by repeating the token request.
- Exit codes: `0` success, `1` transient/partial failure, `2` configuration failure.
- Commit after every task. Conventional Commits format.

## Phase Overview

**Phase 1 (Tasks 1–10)** produces a working watcher that notifies your phone from your laptop. Task 10 is the first end-to-end demo.

**Phase 2 (Tasks 11–15)** wraps it for production: container, Kubernetes, observability, CI, docs.

## File Structure

| File | Responsibility |
|---|---|
| `script/pyproject.toml` | Deps, ruff/mypy/pytest config, `dawatch` entry point |
| `script/src/dawatch/errors.py` | Exception hierarchy; nothing else imports policy from it |
| `script/src/dawatch/config.py` | `Settings` — the only place environment variables are read |
| `script/src/dawatch/logging.py` | structlog setup + secret-redaction processor |
| `script/src/dawatch/models.py` | `Deviation`, `DailyDeviationsPage`, `Token` |
| `script/src/dawatch/store.py` | `SeenStore` + `TokenCache` protocols; SQLite and in-memory impls |
| `script/src/dawatch/auth.py` | `client_credentials` token acquisition, cached via `TokenCache` |
| `script/src/dawatch/client.py` | httpx wrapper: auth header, version header, backoff |
| `script/src/dawatch/sources.py` | `DeviationSource` protocol + `DailyDeviationsSource` |
| `script/src/dawatch/notifier.py` | `Notifier` protocol + `NtfyNotifier`, `ConsoleNotifier` |
| `script/src/dawatch/metrics.py` | `MetricsSink` protocol + `PushgatewaySink`, `NullSink` |
| `script/src/dawatch/service.py` | `WatchService.run()` — orchestration only |
| `script/src/dawatch/cli.py` | Argument parsing, wiring, exit codes |
| `k8s/base/` | namespace, configmap, pvc, cronjob, kustomization |
| `k8s/overlays/{local,prod}/` | image tag, schedule, StorageClass differences |
| `k8s/observability/` | pushgateway, prometheus, grafana + dashboard ConfigMap |

---

## Task 1: Project scaffold and configuration

**Files:**
- Create: `script/pyproject.toml`, `script/src/dawatch/__init__.py`, `script/src/dawatch/errors.py`, `script/src/dawatch/config.py`, `script/.env.example`
- Create: `script/tests/__init__.py`, `script/tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `ConfigError`, `AuthError`, `FetchError`, `NotifyError`, `StoreError` — all subclass `DawatchError(Exception)`
  - `Settings` (pydantic-settings `BaseSettings`) with fields: `client_id: SecretStr`, `client_secret: SecretStr`, `ntfy_url: str`, `ntfy_topic: str`, `db_path: Path`, `env: Literal["dev", "prod"]`, `log_level: str`, `pushgateway_url: str | None`, `http_timeout: float`, `max_retries: int`, `notify_mature: bool`
  - `Settings.load() -> Settings` — classmethod raising `ConfigError` on validation failure

- [ ] **Step 1: Create the package skeleton**

```bash
mkdir -p script/src/dawatch script/tests
touch script/src/dawatch/__init__.py script/tests/__init__.py
```

- [ ] **Step 2: Write `script/pyproject.toml`**

```toml
[project]
name = "dawatch"
version = "0.1.0"
description = "Watch DeviantArt Daily Deviations and push new ones to your phone"
requires-python = ">=3.13"
dependencies = [
    "httpx>=0.28",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "structlog>=24.4",
    "prometheus-client>=0.21",
]

[project.scripts]
dawatch = "dawatch.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-cov>=6.0",
    "respx>=0.22",
    "time-machine>=2.16",
    "ruff>=0.8",
    "mypy>=1.13",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/dawatch"]

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "A", "C4", "SIM", "RUF"]

[tool.mypy]
python_version = "3.13"
strict = true
warn_unreachable = true
files = ["src", "tests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q --cov=dawatch --cov-report=term-missing --cov-fail-under=90"

[tool.coverage.run]
branch = true
source = ["src/dawatch"]
```

- [ ] **Step 3: Install the environment**

```bash
cd script && uv sync --all-groups
```

Expected: a `.venv/` is created and `uv.lock` is written.

- [ ] **Step 4: Write `script/src/dawatch/errors.py`**

```python
"""Exception hierarchy. Every failure the application raises lands here."""


class DawatchError(Exception):
    """Base for every error this application raises deliberately."""


class ConfigError(DawatchError):
    """Configuration is missing or invalid. Retrying will not help."""


class AuthError(DawatchError):
    """The DeviantArt token endpoint refused our credentials."""


class FetchError(DawatchError):
    """Fetching the feed failed after exhausting retries."""


class NotifyError(DawatchError):
    """Delivering one notification failed."""


class StoreError(DawatchError):
    """The seen-store is unreadable or unwritable."""
```

- [ ] **Step 5: Write the failing test `script/tests/test_config.py`**

```python
import pytest

from dawatch.config import Settings
from dawatch.errors import ConfigError

REQUIRED_ENV = {
    "DEVIANTART_CLIENT_ID": "abc123",
    "DEVIANTART_CLIENT_SECRET": "s3cret",
    "DAWATCH_NTFY_TOPIC": "my-topic",
}


def test_load_reads_required_env(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings.load()

    assert settings.client_id.get_secret_value() == "abc123"
    assert settings.client_secret.get_secret_value() == "s3cret"
    assert settings.ntfy_topic == "my-topic"


def test_load_applies_defaults(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings.load()

    assert settings.ntfy_url == "https://ntfy.sh"
    assert settings.env == "prod"
    assert settings.pushgateway_url is None
    assert settings.http_timeout == 10.0
    assert settings.max_retries == 3
    assert settings.notify_mature is False


def test_load_raises_config_error_when_credentials_missing(monkeypatch):
    monkeypatch.delenv("DEVIANTART_CLIENT_ID", raising=False)
    monkeypatch.delenv("DEVIANTART_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("DAWATCH_NTFY_TOPIC", "my-topic")

    with pytest.raises(ConfigError) as exc_info:
        Settings.load()

    assert "DEVIANTART_CLIENT_ID" in str(exc_info.value)


def test_secrets_are_not_exposed_by_repr(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    rendered = repr(Settings.load())

    assert "s3cret" not in rendered
    assert "abc123" not in rendered
```

Note: `Settings.load()` must not read a `.env` file during tests, or a
developer's real `.env` would leak into assertions. The implementation below
reads `.env` only when it exists **and** `DAWATCH_ENV_FILE` is not set to
`""`; tests set no `.env`, and CI has none. Keep the repo's `.env` gitignored.

- [ ] **Step 6: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_config.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.config'`

- [ ] **Step 7: Write `script/src/dawatch/config.py`**

```python
"""Application configuration. The only place environment variables are read."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from dawatch.errors import ConfigError


class Settings(BaseSettings):
    """Runtime configuration, populated from the environment.

    Credentials use the ``DEVIANTART_`` prefix because they are issued by
    DeviantArt; everything else uses ``DAWATCH_`` because it is ours.
    """

    model_config = SettingsConfigDict(
        env_prefix="DAWATCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    client_id: SecretStr = Field(validation_alias="DEVIANTART_CLIENT_ID")
    client_secret: SecretStr = Field(validation_alias="DEVIANTART_CLIENT_SECRET")

    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str

    db_path: Path = Path("/data/dawatch.db")

    env: Literal["dev", "prod"] = "prod"
    log_level: str = "INFO"

    pushgateway_url: str | None = None

    http_timeout: float = 10.0
    max_retries: int = 3
    notify_mature: bool = False

    @classmethod
    def load(cls) -> Self:
        """Build settings from the environment.

        Raises:
            ConfigError: if any required value is missing or malformed. The
                message names the offending variables so an operator can fix
                the deployment without reading source.
        """
        try:
            return cls()  # type: ignore[call-arg]
        except ValidationError as exc:
            names = [cls._env_name_for(error["loc"]) for error in exc.errors()]
            raise ConfigError(
                f"Invalid configuration. Check these environment variables: {', '.join(names)}"
            ) from exc

    @classmethod
    def _env_name_for(cls, loc: tuple[int | str, ...]) -> str:
        """Map a pydantic error location back to its environment variable name."""
        if not loc:
            return "<unknown>"
        field_name = str(loc[0])
        field = cls.model_fields.get(field_name)
        alias = getattr(field, "validation_alias", None) if field else None
        if isinstance(alias, str):
            return alias
        return f"DAWATCH_{field_name.upper()}"
```

- [ ] **Step 8: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_config.py -v --no-cov`
Expected: 4 passed

- [ ] **Step 9: Write `script/.env.example`**

```bash
# DeviantArt OAuth2 application credentials.
# Register an app at https://www.deviantart.com/developers/apps
DEVIANTART_CLIENT_ID=your_client_id_here
DEVIANTART_CLIENT_SECRET=your_client_secret_here

# ntfy topic to publish to. Treat this as a secret: anyone who knows the
# topic name can read your notifications on the public ntfy.sh server.
DAWATCH_NTFY_TOPIC=change-me-to-something-unguessable
DAWATCH_NTFY_URL=https://ntfy.sh

# Local development overrides.
DAWATCH_DB_PATH=./data/dawatch.db
DAWATCH_ENV=dev
DAWATCH_LOG_LEVEL=DEBUG

# Leave unset to disable metrics.
# DAWATCH_PUSHGATEWAY_URL=http://localhost:9091
```

- [ ] **Step 10: Verify lint and types pass**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: all three clean. If `ruff format --check` fails, run `uv run ruff format .` and re-check.

- [ ] **Step 11: Commit**

```bash
git add script/ && git commit -m "feat: add project scaffold and settings

Settings is the only place environment variables are read. Credentials
are SecretStr so an accidental repr cannot leak them, and load() converts
pydantic's validation errors into a ConfigError naming the offending
environment variables."
```

---

## Task 2: Structured logging with secret redaction

**Files:**
- Create: `script/src/dawatch/logging.py`
- Create: `script/tests/test_logging.py`

**Interfaces:**
- Consumes: `Settings` from Task 1
- Produces:
  - `redact_secrets(logger, method_name, event_dict) -> MutableMapping[str, Any]` — a structlog processor
  - `configure_logging(settings: Settings) -> None`
  - `SENSITIVE_KEYS: frozenset[str]`

- [ ] **Step 1: Write the failing test `script/tests/test_logging.py`**

```python
import structlog

from dawatch.config import Settings
from dawatch.logging import configure_logging, redact_secrets


def test_redacts_sensitive_keys():
    event = {"event": "auth", "access_token": "abc", "client_secret": "xyz"}

    result = redact_secrets(None, "info", event)

    assert result["access_token"] == "***REDACTED***"
    assert result["client_secret"] == "***REDACTED***"


def test_redaction_is_case_insensitive_and_matches_substrings():
    event = {"event": "req", "Authorization": "Bearer abc", "DA_CLIENT_SECRET": "xyz"}

    result = redact_secrets(None, "info", event)

    assert result["Authorization"] == "***REDACTED***"
    assert result["DA_CLIENT_SECRET"] == "***REDACTED***"


def test_leaves_innocuous_keys_untouched():
    event = {"event": "fetched", "count": 7, "deviationid": "ABC-123"}

    result = redact_secrets(None, "info", event)

    assert result["count"] == 7
    assert result["deviationid"] == "ABC-123"


def test_configure_logging_emits_json_in_prod(monkeypatch, capsys):
    monkeypatch.setenv("DEVIANTART_CLIENT_ID", "abc")
    monkeypatch.setenv("DEVIANTART_CLIENT_SECRET", "xyz")
    monkeypatch.setenv("DAWATCH_NTFY_TOPIC", "t")
    monkeypatch.setenv("DAWATCH_ENV", "prod")

    configure_logging(Settings.load())
    structlog.get_logger().info("hello", access_token="leak-me")

    captured = capsys.readouterr().out
    assert '"event": "hello"' in captured or '"event":"hello"' in captured
    assert "leak-me" not in captured
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_logging.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.logging'`

- [ ] **Step 3: Write `script/src/dawatch/logging.py`**

```python
"""Structured logging.

structlog wraps the standard library, so httpx's own loggers are captured by
the same configuration rather than bypassing it.
"""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from dawatch.config import Settings

SENSITIVE_KEYS = frozenset(
    {"access_token", "client_secret", "client_id", "authorization", "token", "password"}
)

REDACTED = "***REDACTED***"


def redact_secrets(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace the value of any sensitive-looking key with a placeholder.

    Matching is case-insensitive and substring-based, so ``DA_CLIENT_SECRET``
    and ``Authorization`` are both caught. This is the last line of defence:
    values should not reach a log call in the first place.
    """
    for key in list(event_dict):
        lowered = key.lower()
        if any(sensitive in lowered for sensitive in SENSITIVE_KEYS):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger for this process."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.env == "dev"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
```

`cache_logger_on_first_use=False` matters: tests reconfigure logging between
cases, and caching would freeze the first configuration for the whole session.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_logging.py -v --no-cov`
Expected: 4 passed

- [ ] **Step 5: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add script/ && git commit -m "feat: add structured logging with secret redaction

A redaction processor blanks any sensitive-looking key before rendering,
so a token cannot reach a log line even through an exception repr. JSON in
prod, console renderer in dev, from one switch."
```

---

## Task 3: API models

**Files:**
- Create: `script/src/dawatch/models.py`
- Create: `script/tests/test_models.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Author` with `username: str`
  - `MediaRef` with `src: str | None`
  - `Deviation` with `deviationid: str`, `title: str`, `url: str | None`, `author: Author`, `is_mature: bool`, `published_time: str | None`, `preview: MediaRef | None`, `content: MediaRef | None`, and properties `author_name: str`, `image_url: str | None`
  - `DailyDeviationsPage` with `results: list[Deviation]`, `has_more: bool`
  - `Token` with `access_token: str`, `expires_at: datetime`, and `is_valid(now: datetime, leeway_seconds: int = 60) -> bool`
  - `Token.from_response(payload: dict[str, Any], now: datetime) -> Token`

- [ ] **Step 1: Write the failing test `script/tests/test_models.py`**

```python
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from dawatch.models import DailyDeviationsPage, Deviation, Token

MINIMAL_DEVIATION = {"deviationid": "ABC-123"}

FULL_DEVIATION = {
    "deviationid": "DEF-456",
    "title": "Neon Alley",
    "url": "https://www.deviantart.com/artist/art/neon-alley-1",
    "author": {"userid": "u1", "username": "artist", "usericon": "https://x/i.png"},
    "is_mature": True,
    "published_time": "1724371200",
    "preview": {"src": "https://images/preview.jpg", "height": 400, "width": 600},
    "content": {"src": "https://images/full.jpg", "filesize": 90210},
    "stats": {"comments": 3, "favourites": 40},
    "some_future_field": {"nested": True},
}


def test_parses_a_full_deviation():
    deviation = Deviation.model_validate(FULL_DEVIATION)

    assert deviation.deviationid == "DEF-456"
    assert deviation.title == "Neon Alley"
    assert deviation.author_name == "artist"
    assert deviation.is_mature is True


def test_tolerates_unknown_fields():
    """The API adds fields over time; that must never break a scheduled run."""
    deviation = Deviation.model_validate(FULL_DEVIATION)

    assert not hasattr(deviation, "some_future_field")


def test_applies_defaults_for_a_minimal_deviation():
    deviation = Deviation.model_validate(MINIMAL_DEVIATION)

    assert deviation.title == "Untitled"
    assert deviation.author_name == "unknown"
    assert deviation.image_url is None
    assert deviation.is_mature is False


def test_deviationid_is_required():
    with pytest.raises(ValidationError):
        Deviation.model_validate({"title": "no id"})


def test_image_url_prefers_preview_over_content():
    deviation = Deviation.model_validate(FULL_DEVIATION)

    assert deviation.image_url == "https://images/preview.jpg"


def test_image_url_falls_back_to_content():
    payload = {"deviationid": "X", "content": {"src": "https://images/full.jpg"}}

    assert Deviation.model_validate(payload).image_url == "https://images/full.jpg"


def test_parses_a_page():
    page = DailyDeviationsPage.model_validate(
        {"results": [MINIMAL_DEVIATION, FULL_DEVIATION], "has_more": False}
    )

    assert len(page.results) == 2
    assert page.has_more is False


def test_page_defaults_to_empty_results():
    page = DailyDeviationsPage.model_validate({})

    assert page.results == []


def test_token_from_response_computes_expiry():
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)

    token = Token.from_response(
        {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}, now
    )

    assert token.access_token == "tok"
    assert token.expires_at == now + timedelta(seconds=3600)


def test_token_is_valid_well_before_expiry():
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    token = Token(access_token="tok", expires_at=now + timedelta(seconds=3600))

    assert token.is_valid(now) is True


def test_token_is_invalid_inside_the_leeway_window():
    """A token expiring in 30s is treated as dead, so a slow run cannot 401."""
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    token = Token(access_token="tok", expires_at=now + timedelta(seconds=30))

    assert token.is_valid(now, leeway_seconds=60) is False


def test_token_is_invalid_after_expiry():
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    token = Token(access_token="tok", expires_at=now - timedelta(seconds=1))

    assert token.is_valid(now) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_models.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.models'`

- [ ] **Step 3: Write `script/src/dawatch/models.py`**

```python
"""Pydantic models for the DeviantArt API surface we consume.

Every model ignores unknown fields. The API gains fields over time, and a
scheduled job must not start failing because DeviantArt shipped a feature.
"""

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_TOLERANT = ConfigDict(extra="ignore")


class Author(BaseModel):
    model_config = _TOLERANT

    userid: str | None = None
    username: str = "unknown"


class MediaRef(BaseModel):
    """A preview or full-content image reference."""

    model_config = _TOLERANT

    src: str | None = None


class Deviation(BaseModel):
    model_config = _TOLERANT

    deviationid: str
    title: str = "Untitled"
    url: str | None = None
    author: Author = Field(default_factory=Author)
    is_mature: bool = False
    published_time: str | None = None
    preview: MediaRef | None = None
    content: MediaRef | None = None

    @property
    def author_name(self) -> str:
        return self.author.username

    @property
    def image_url(self) -> str | None:
        """Best available image for a notification attachment.

        The preview is preferred: it is smaller, and a notification thumbnail
        does not benefit from a multi-megabyte original.
        """
        if self.preview is not None and self.preview.src:
            return self.preview.src
        if self.content is not None and self.content.src:
            return self.content.src
        return None


class DailyDeviationsPage(BaseModel):
    model_config = _TOLERANT

    results: list[Deviation] = Field(default_factory=list)
    has_more: bool = False


class Token(BaseModel):
    """An OAuth2 access token and the moment it stops being usable."""

    access_token: str
    expires_at: datetime

    @classmethod
    def from_response(cls, payload: dict[str, Any], now: datetime) -> "Token":
        expires_in = int(payload.get("expires_in", 3600))
        return cls(
            access_token=str(payload["access_token"]),
            expires_at=now + timedelta(seconds=expires_in),
        )

    def is_valid(self, now: datetime, leeway_seconds: int = 60) -> bool:
        """True if the token has more than ``leeway_seconds`` of life left.

        The leeway prevents a token that expires mid-run from causing a 401
        halfway through a batch of notifications.
        """
        return self.expires_at - timedelta(seconds=leeway_seconds) > now
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_models.py -v --no-cov`
Expected: 12 passed

- [ ] **Step 5: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add script/ && git commit -m "feat: add API models tolerant of unknown fields

Deviation, DailyDeviationsPage and Token. Every model ignores extra keys so
a DeviantArt API addition cannot break a scheduled run, and Token.is_valid
applies a leeway window so a token cannot expire mid-batch."
```

---

## Task 4: SQLite seen-store and token cache

**Files:**
- Create: `script/src/dawatch/store.py`
- Create: `script/tests/test_store.py`

**Interfaces:**
- Consumes: `Deviation`, `Token` from Task 3; `StoreError` from Task 1
- Produces:
  - `SeenStore` protocol: `has_seen(deviationid: str) -> bool`, `mark_seen(deviation: Deviation, notified: bool) -> None`, `is_empty() -> bool`
  - `TokenCache` protocol: `load_token() -> Token | None`, `save_token(token: Token) -> None`
  - `SqliteStore(db_path: Path)` implementing both, plus `close() -> None` and context-manager support
  - `InMemoryStore()` implementing both, for tests

- [ ] **Step 1: Write the failing test `script/tests/test_store.py`**

```python
from datetime import UTC, datetime, timedelta

import pytest

from dawatch.models import Deviation, Token
from dawatch.store import InMemoryStore, SqliteStore

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def make_deviation(deviationid: str = "ABC-123") -> Deviation:
    return Deviation.model_validate(
        {
            "deviationid": deviationid,
            "title": "Neon Alley",
            "url": "https://example.invalid/art",
            "author": {"username": "artist"},
        }
    )


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "test.db") as store:
        yield store


def test_new_store_is_empty(store):
    assert store.is_empty() is True


def test_unseen_deviation_reports_false(store):
    assert store.has_seen("ABC-123") is False


def test_marked_deviation_reports_true(store):
    store.mark_seen(make_deviation(), notified=True)

    assert store.has_seen("ABC-123") is True
    assert store.is_empty() is False


def test_mark_seen_is_idempotent(store):
    """A retry after a crash must not raise on the primary key."""
    store.mark_seen(make_deviation(), notified=True)
    store.mark_seen(make_deviation(), notified=True)

    assert store.has_seen("ABC-123") is True


def test_state_survives_reopening(tmp_path):
    """The whole point of the store: a new process sees the old decisions."""
    db_path = tmp_path / "test.db"
    with SqliteStore(db_path) as first:
        first.mark_seen(make_deviation(), notified=True)

    with SqliteStore(db_path) as second:
        assert second.has_seen("ABC-123") is True


def test_seeded_rows_are_distinguishable_from_notified_rows(store):
    store.mark_seen(make_deviation("SEEDED"), notified=False)
    store.mark_seen(make_deviation("PUSHED"), notified=True)

    assert store.notified_at("SEEDED") is None
    assert store.notified_at("PUSHED") is not None


def test_token_cache_is_empty_initially(store):
    assert store.load_token() is None


def test_token_round_trips(store):
    token = Token(access_token="tok", expires_at=NOW + timedelta(seconds=3600))

    store.save_token(token)
    loaded = store.load_token()

    assert loaded is not None
    assert loaded.access_token == "tok"
    assert loaded.expires_at == token.expires_at


def test_saving_a_token_replaces_the_previous_one(store):
    store.save_token(Token(access_token="old", expires_at=NOW))
    store.save_token(Token(access_token="new", expires_at=NOW + timedelta(seconds=10)))

    loaded = store.load_token()
    assert loaded is not None
    assert loaded.access_token == "new"


def test_creates_parent_directories(tmp_path):
    db_path = tmp_path / "nested" / "deeper" / "test.db"

    with SqliteStore(db_path) as store:
        assert store.is_empty() is True

    assert db_path.exists()


def test_in_memory_store_matches_sqlite_behaviour():
    """The test double must not diverge from the real thing."""
    store = InMemoryStore()

    assert store.is_empty() is True
    assert store.has_seen("ABC-123") is False

    store.mark_seen(make_deviation(), notified=True)

    assert store.has_seen("ABC-123") is True
    assert store.is_empty() is False
    assert store.load_token() is None

    token = Token(access_token="tok", expires_at=NOW)
    store.save_token(token)
    loaded = store.load_token()
    assert loaded is not None
    assert loaded.access_token == "tok"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_store.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.store'`

- [ ] **Step 3: Write `script/src/dawatch/store.py`**

```python
"""Persistent state: which deviations were seen, and the cached access token.

SQLite is used through the standard library. WAL mode plus a busy timeout
makes concurrent access safe on local storage. The database must never live
on NFS, where SQLite's advisory locking is unreliable.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from dawatch.errors import StoreError
from dawatch.models import Deviation, Token

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_deviations (
    deviationid   TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    author        TEXT NOT NULL,
    url           TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    notified_at   TEXT
);

CREATE TABLE IF NOT EXISTS token_cache (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    access_token TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
"""


@runtime_checkable
class SeenStore(Protocol):
    """Remembers which deviations have already been handled."""

    def has_seen(self, deviationid: str) -> bool: ...

    def mark_seen(self, deviation: Deviation, notified: bool) -> None: ...

    def is_empty(self) -> bool: ...


@runtime_checkable
class TokenCache(Protocol):
    """Persists an access token across short-lived process runs."""

    def load_token(self) -> Token | None: ...

    def save_token(self, token: Token) -> None: ...


class SqliteStore:
    """SQLite-backed :class:`SeenStore` and :class:`TokenCache`."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"Cannot open database at {self._db_path}: {exc}") from exc

        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def has_seen(self, deviationid: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_deviations WHERE deviationid = ?", (deviationid,)
        ).fetchone()
        return row is not None

    def mark_seen(self, deviation: Deviation, notified: bool) -> None:
        """Record a deviation as handled.

        Uses INSERT OR REPLACE so that re-running after a crash is safe.
        ``notified`` distinguishes a genuine push from a silent seeding write.
        """
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO seen_deviations
                (deviationid, title, author, url, first_seen_at, notified_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                deviation.deviationid,
                deviation.title,
                deviation.author_name,
                deviation.url or "",
                now,
                now if notified else None,
            ),
        )

    def notified_at(self, deviationid: str) -> str | None:
        row = self._conn.execute(
            "SELECT notified_at FROM seen_deviations WHERE deviationid = ?", (deviationid,)
        ).fetchone()
        if row is None:
            return None
        value = row["notified_at"]
        return str(value) if value is not None else None

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT 1 FROM seen_deviations LIMIT 1").fetchone()
        return row is None

    def load_token(self) -> Token | None:
        row = self._conn.execute(
            "SELECT access_token, expires_at FROM token_cache WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return Token(
            access_token=str(row["access_token"]),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
        )

    def save_token(self, token: Token) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO token_cache (id, access_token, expires_at)
            VALUES (1, ?, ?)
            """,
            (token.access_token, token.expires_at.isoformat()),
        )


class InMemoryStore:
    """Non-persistent test double with the same semantics as SqliteStore."""

    def __init__(self) -> None:
        self._seen: dict[str, str | None] = {}
        self._token: Token | None = None

    def has_seen(self, deviationid: str) -> bool:
        return deviationid in self._seen

    def mark_seen(self, deviation: Deviation, notified: bool) -> None:
        self._seen[deviation.deviationid] = datetime.now(UTC).isoformat() if notified else None

    def notified_at(self, deviationid: str) -> str | None:
        return self._seen.get(deviationid)

    def is_empty(self) -> bool:
        return not self._seen

    def load_token(self) -> Token | None:
        return self._token

    def save_token(self, token: Token) -> None:
        self._token = token
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_store.py -v --no-cov`
Expected: 11 passed

- [ ] **Step 5: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add script/ && git commit -m "feat: add SQLite seen-store and token cache

One SQLite file backs both the seen-store and the token cache, so a
short-lived CronJob run reuses a token instead of re-authenticating every
time. WAL mode and a busy timeout keep concurrent access safe; INSERT OR
REPLACE makes a post-crash retry idempotent."
```

---

## Task 5: OAuth2 client credentials authentication

**Files:**
- Create: `script/src/dawatch/auth.py`
- Create: `script/tests/test_auth.py`

**Interfaces:**
- Consumes: `Token` (Task 3), `TokenCache` (Task 4), `AuthError` (Task 1)
- Produces:
  - `TOKEN_URL: str = "https://www.deviantart.com/oauth2/token"`
  - `TokenProvider` protocol: `token() -> str`, `invalidate() -> None`
  - `DeviantArtAuth(http: httpx.Client, client_id: str, client_secret: str, cache: TokenCache)` implementing it

- [ ] **Step 1: Write the failing test `script/tests/test_auth.py`**

```python
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
import time_machine

from dawatch.auth import TOKEN_URL, DeviantArtAuth
from dawatch.errors import AuthError
from dawatch.models import Token
from dawatch.store import InMemoryStore

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)

TOKEN_RESPONSE = {
    "status": "success",
    "access_token": "fresh-token",
    "token_type": "Bearer",
    "expires_in": 3600,
}


@pytest.fixture
def cache():
    return InMemoryStore()


@pytest.fixture
def auth(cache):
    with httpx.Client() as http:
        yield DeviantArtAuth(http, "cid", "csecret", cache)


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_requests_a_token_when_cache_is_empty(auth, cache):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    assert auth.token() == "fresh-token"
    assert route.called


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_sends_client_credentials_grant(auth):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    auth.token()

    body = route.calls.last.request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body
    assert "client_secret=csecret" in body


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_persists_the_token_to_the_cache(auth, cache):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    auth.token()

    stored = cache.load_token()
    assert stored is not None
    assert stored.access_token == "fresh-token"
    assert stored.expires_at == NOW + timedelta(seconds=3600)


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_reuses_a_cached_token_without_calling_the_network(cache):
    cache.save_token(Token(access_token="cached", expires_at=NOW + timedelta(seconds=3600)))
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    with httpx.Client() as http:
        assert DeviantArtAuth(http, "cid", "csecret", cache).token() == "cached"

    assert not route.called


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_replaces_an_expired_cached_token(cache):
    cache.save_token(Token(access_token="stale", expires_at=NOW - timedelta(seconds=1)))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    with httpx.Client() as http:
        assert DeviantArtAuth(http, "cid", "csecret", cache).token() == "fresh-token"


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_caches_within_a_single_run(auth):
    """Two calls in one process must not hit the network twice."""
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    auth.token()
    auth.token()

    assert route.call_count == 1


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_invalidate_forces_a_new_token(auth):
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    auth.token()
    auth.invalidate()
    auth.token()

    assert route.call_count == 2


@respx.mock
def test_rejected_credentials_raise_auth_error(auth):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )

    with pytest.raises(AuthError) as exc_info:
        auth.token()

    assert "csecret" not in str(exc_info.value)


@respx.mock
def test_malformed_token_response_raises_auth_error(auth):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"status": "success"}))

    with pytest.raises(AuthError):
        auth.token()


@respx.mock
def test_transport_failure_raises_auth_error(auth):
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(AuthError):
        auth.token()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_auth.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.auth'`

- [ ] **Step 3: Write `script/src/dawatch/auth.py`**

```python
"""OAuth2 client credentials authentication.

This grant issues no refresh token, so expiry is handled by repeating the
token request. Tokens are cached across process runs, because a scheduled
one-shot job would otherwise authenticate on every single invocation.
"""

from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import structlog

from dawatch.errors import AuthError
from dawatch.models import Token
from dawatch.store import TokenCache

TOKEN_URL = "https://www.deviantart.com/oauth2/token"

log = structlog.get_logger(__name__)


class TokenProvider(Protocol):
    def token(self) -> str: ...

    def invalidate(self) -> None: ...


class DeviantArtAuth:
    """Supplies a bearer token, from cache when possible."""

    def __init__(
        self,
        http: httpx.Client,
        client_id: str,
        client_secret: str,
        cache: TokenCache,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._cache = cache
        self._current: Token | None = None

    def token(self) -> str:
        """Return a usable access token, fetching a new one only if needed."""
        now = datetime.now(UTC)

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
        log.info("token.refreshed", expires_at=fresh.expires_at.isoformat())
        return fresh.access_token

    def invalidate(self) -> None:
        """Drop the in-process token so the next call re-authenticates.

        Called when the API rejects a token we believed was live, which can
        happen if DeviantArt revokes it early.
        """
        self._current = None

    def _request_token(self, now: datetime) -> Token:
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"Could not reach the token endpoint: {type(exc).__name__}") from exc

        if response.status_code != httpx.codes.OK:
            # The body is deliberately not included: it can echo credentials.
            raise AuthError(
                f"Token endpoint rejected the client credentials "
                f"(HTTP {response.status_code}). Check DEVIANTART_CLIENT_ID and "
                f"DEVIANTART_CLIENT_SECRET."
            )

        try:
            payload: dict[str, Any] = response.json()
            return Token.from_response(payload, now)
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthError("Token endpoint returned a malformed response") from exc
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_auth.py -v --no-cov`
Expected: 10 passed

- [ ] **Step 5: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add script/ && git commit -m "feat: add client credentials authentication with token caching

The grant issues no refresh token, so expiry means repeating the request.
Tokens persist in the store between runs, since a one-shot scheduled job
would otherwise authenticate every invocation. Error messages never echo
the response body, which can contain credentials."
```

---

## Task 6: HTTP client with backoff

**Files:**
- Create: `script/src/dawatch/client.py`
- Create: `script/tests/test_client.py`

**Interfaces:**
- Consumes: `TokenProvider` (Task 5), `FetchError` (Task 1)
- Produces:
  - `API_BASE: str = "https://www.deviantart.com/api/v1/oauth2"`
  - `API_VERSION: str = "20240701"`
  - `DeviantArtClient(http: httpx.Client, auth: TokenProvider, max_retries: int = 3, sleep: Callable[[float], None] = time.sleep)`
  - `DeviantArtClient.get_json(path: str, params: dict[str, str] | None = None) -> dict[str, Any]`

- [ ] **Step 1: Write the failing test `script/tests/test_client.py`**

```python
import httpx
import pytest
import respx

from dawatch.client import API_BASE, API_VERSION, DeviantArtClient
from dawatch.errors import FetchError

FEED_URL = f"{API_BASE}/browse/dailydeviations"


class StubAuth:
    def __init__(self) -> None:
        self.tokens = ["token-1", "token-2"]
        self.invalidated = 0

    def token(self) -> str:
        return self.tokens[min(self.invalidated, len(self.tokens) - 1)]

    def invalidate(self) -> None:
        self.invalidated += 1


@pytest.fixture
def auth():
    return StubAuth()


@pytest.fixture
def client(auth):
    """No real sleeping: retry tests must not take seconds of wall clock."""
    with httpx.Client() as http:
        yield DeviantArtClient(http, auth, max_retries=3, sleep=lambda _: None)


@respx.mock
def test_returns_decoded_json(client):
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    assert client.get_json("browse/dailydeviations") == {"results": []}


@respx.mock
def test_sends_bearer_token_and_version_header(client):
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={}))

    client.get_json("browse/dailydeviations")

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer token-1"
    assert request.headers["dA-minor-version"] == API_VERSION


@respx.mock
def test_forwards_query_parameters(client):
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={}))

    client.get_json("browse/dailydeviations", {"date": "2026-08-23"})

    assert route.calls.last.request.url.params["date"] == "2026-08-23"


@respx.mock
def test_retries_on_server_error_then_succeeds(client):
    route = respx.get(FEED_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"results": [{"deviationid": "A"}]}),
        ]
    )

    result = client.get_json("browse/dailydeviations")

    assert route.call_count == 2
    assert result["results"][0]["deviationid"] == "A"


@respx.mock
def test_retries_on_connect_error(client):
    route = respx.get(FEED_URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})]
    )

    client.get_json("browse/dailydeviations")

    assert route.call_count == 2


@respx.mock
def test_honours_retry_after_header(auth):
    delays: list[float] = []
    respx.get(FEED_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={}),
        ]
    )

    with httpx.Client() as http:
        client = DeviantArtClient(http, auth, max_retries=3, sleep=delays.append)
        client.get_json("browse/dailydeviations")

    assert delays == [7.0]


@respx.mock
def test_backs_off_exponentially_without_retry_after(auth):
    delays: list[float] = []
    respx.get(FEED_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200, json={})]
    )

    with httpx.Client() as http:
        client = DeviantArtClient(http, auth, max_retries=3, sleep=delays.append)
        client.get_json("browse/dailydeviations")

    assert delays == [1.0, 2.0]


@respx.mock
def test_gives_up_after_max_retries(client):
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(FetchError) as exc_info:
        client.get_json("browse/dailydeviations")

    assert route.call_count == 3
    assert "503" in str(exc_info.value)


@respx.mock
def test_retries_once_on_401_after_invalidating_the_token(client, auth):
    """A revoked token deserves exactly one re-auth, not a retry storm."""
    route = respx.get(FEED_URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={})]
    )

    client.get_json("browse/dailydeviations")

    assert auth.invalidated == 1
    assert route.call_count == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer token-2"


@respx.mock
def test_does_not_retry_a_client_error(client):
    """A 400 is a defect in our request; retrying just wastes quota."""
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(400))

    with pytest.raises(FetchError):
        client.get_json("browse/dailydeviations")

    assert route.call_count == 1


@respx.mock
def test_malformed_json_raises_fetch_error(client):
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=b"<html>nope</html>"))

    with pytest.raises(FetchError):
        client.get_json("browse/dailydeviations")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_client.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.client'`

- [ ] **Step 3: Write `script/src/dawatch/client.py`**

```python
"""HTTP access to the DeviantArt API.

Retries cover connection errors, 429, and 5xx. They deliberately do not cover
other 4xx responses: those indicate a defect in our request, and retrying
would burn quota without any chance of succeeding.
"""

import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from dawatch.auth import TokenProvider
from dawatch.errors import FetchError

API_BASE = "https://www.deviantart.com/api/v1/oauth2"
API_VERSION = "20240701"

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

log = structlog.get_logger(__name__)


class DeviantArtClient:
    """Authenticated JSON GETs against the DeviantArt API."""

    def __init__(
        self,
        http: httpx.Client,
        auth: TokenProvider,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._auth = auth
        self._max_retries = max(1, max_retries)
        self._sleep = sleep

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """GET ``path`` and return the decoded JSON body.

        Raises:
            FetchError: on a non-retryable status, exhausted retries, or a
                body that is not JSON.
        """
        url = f"{API_BASE}/{path.lstrip('/')}"
        reauthed = False
        last_problem = "unknown"

        for attempt in range(self._max_retries):
            try:
                response = self._http.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._auth.token()}",
                        "dA-minor-version": API_VERSION,
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                last_problem = type(exc).__name__
                log.warning("fetch.transport_error", attempt=attempt + 1, error=last_problem)
                self._wait(attempt, None)
                continue

            status = response.status_code

            if status == httpx.codes.OK:
                return self._decode(response)

            if status == httpx.codes.UNAUTHORIZED and not reauthed:
                # The token was accepted at issue time but is no longer
                # valid. Re-authenticate once; a loop here would hammer the
                # token endpoint on genuinely bad credentials.
                log.info("fetch.reauthenticating")
                reauthed = True
                self._auth.invalidate()
                continue

            if status not in RETRYABLE_STATUS:
                raise FetchError(f"GET {path} failed with HTTP {status}")

            last_problem = f"HTTP {status}"
            log.warning("fetch.retryable_status", attempt=attempt + 1, status=status)
            self._wait(attempt, response.headers.get("Retry-After"))

        raise FetchError(f"GET {path} failed after {self._max_retries} attempts: {last_problem}")

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        """Sleep before the next attempt, preferring the server's own advice."""
        if retry_after is not None:
            try:
                self._sleep(float(retry_after))
                return
            except ValueError:
                # Retry-After may be an HTTP-date; fall back to backoff.
                pass
        self._sleep(float(2**attempt))

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise FetchError("API returned a body that is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise FetchError(f"API returned {type(payload).__name__}, expected an object")
        return payload
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_client.py -v --no-cov`
Expected: 11 passed

- [ ] **Step 5: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add script/ && git commit -m "feat: add HTTP client with bounded backoff

Retries cover connection errors, 429 and 5xx, honouring Retry-After when the
server sends it. Other 4xx responses are not retried: they indicate a defect
in the request, so retrying would burn quota for nothing. A 401 triggers
exactly one re-authentication."
```

---

## Task 7: Daily Deviations source

**Files:**
- Create: `script/src/dawatch/sources.py`
- Create: `script/tests/test_sources.py`

**Interfaces:**
- Consumes: `DeviantArtClient` (Task 6), `DailyDeviationsPage`, `Deviation` (Task 3), `FetchError` (Task 1)
- Produces:
  - `DeviationSource` protocol: `fetch(date: str | None = None) -> list[Deviation]`
  - `DailyDeviationsSource(client: DeviantArtClient)` implementing it

- [ ] **Step 1: Write the failing test `script/tests/test_sources.py`**

```python
from typing import Any

import pytest

from dawatch.errors import FetchError
from dawatch.sources import DailyDeviationsSource

PAYLOAD: dict[str, Any] = {
    "has_more": False,
    "results": [
        {"deviationid": "A", "title": "First", "author": {"username": "alice"}},
        {"deviationid": "B", "title": "Second", "author": {"username": "bob"}},
    ],
}


class StubClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append((path, params))
        return self.payload


def test_returns_parsed_deviations():
    source = DailyDeviationsSource(StubClient(PAYLOAD))

    deviations = source.fetch()

    assert [d.deviationid for d in deviations] == ["A", "B"]
    assert deviations[0].author_name == "alice"


def test_requests_the_daily_deviations_path():
    client = StubClient(PAYLOAD)

    DailyDeviationsSource(client).fetch()

    assert client.calls[0][0] == "browse/dailydeviations"


def test_sends_no_params_when_no_date_given():
    """The endpoint defaults to today; sending date= explicitly is noise."""
    client = StubClient(PAYLOAD)

    DailyDeviationsSource(client).fetch()

    assert client.calls[0][1] is None


def test_sends_the_date_when_given():
    client = StubClient(PAYLOAD)

    DailyDeviationsSource(client).fetch("2026-08-01")

    assert client.calls[0][1] == {"date": "2026-08-01"}


def test_rejects_a_malformed_date_before_making_a_request():
    client = StubClient(PAYLOAD)

    with pytest.raises(FetchError) as exc_info:
        DailyDeviationsSource(client).fetch("01-08-2026")

    assert client.calls == []
    assert "YYYY-MM-DD" in str(exc_info.value)


def test_handles_an_empty_feed():
    source = DailyDeviationsSource(StubClient({"results": [], "has_more": False}))

    assert source.fetch() == []


def test_unparseable_payload_raises_fetch_error():
    source = DailyDeviationsSource(StubClient({"results": "not-a-list"}))

    with pytest.raises(FetchError):
        source.fetch()


def test_skips_a_single_malformed_result_rather_than_failing_the_run():
    """One bad row must not cost the user every other notification."""
    payload = {"results": [{"deviationid": "A"}, {"title": "no id"}], "has_more": False}

    deviations = DailyDeviationsSource(StubClient(payload)).fetch()

    assert [d.deviationid for d in deviations] == ["A"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_sources.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.sources'`

- [ ] **Step 3: Write `script/src/dawatch/sources.py`**

```python
"""Sources of deviations.

Only the Daily Deviations feed is implemented. The 'artists you watch' feed
needs the OAuth2 authorization code flow, so it becomes a second
DeviationSource later without any change to the orchestration layer.
"""

import re
from datetime import datetime
from typing import Any, Protocol

import structlog
from pydantic import ValidationError

from dawatch.errors import FetchError
from dawatch.models import Deviation

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

log = structlog.get_logger(__name__)


class JsonClient(Protocol):
    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]: ...


class DeviationSource(Protocol):
    def fetch(self, date: str | None = None) -> list[Deviation]: ...


class DailyDeviationsSource:
    """The staff-picked Daily Deviations for one day.

    The endpoint takes only a ``date``. It has no limit and no offset, so a
    single request returns the whole day and there is no pagination.
    """

    PATH = "browse/dailydeviations"

    def __init__(self, client: JsonClient) -> None:
        self._client = client

    def fetch(self, date: str | None = None) -> list[Deviation]:
        """Return the day's deviations, newest feed first.

        Raises:
            FetchError: if ``date`` is malformed or the payload cannot be
                interpreted as a feed at all.
        """
        params = {"date": self._validate_date(date)} if date is not None else None

        payload = self._client.get_json(self.PATH, params)
        raw_results = payload.get("results", [])

        if not isinstance(raw_results, list):
            raise FetchError(
                f"Feed payload had {type(raw_results).__name__} results, expected a list"
            )

        deviations: list[Deviation] = []
        for raw in raw_results:
            try:
                deviations.append(Deviation.model_validate(raw))
            except ValidationError:
                # One unusable row should not cost the user every other
                # notification in the batch.
                log.warning("source.skipped_malformed_result")

        log.info("source.fetched", count=len(deviations), date=date or "today")
        return deviations

    @staticmethod
    def _validate_date(date: str) -> str:
        if not DATE_PATTERN.match(date):
            raise FetchError(f"Date {date!r} is not in YYYY-MM-DD format")
        try:
            datetime.strptime(date, "%Y-%m-%d")  # noqa: DTZ007 - a calendar date, not an instant
        except ValueError as exc:
            raise FetchError(f"Date {date!r} is not a real calendar date") from exc
        return date
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_sources.py -v --no-cov`
Expected: 8 passed

- [ ] **Step 5: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add script/ && git commit -m "feat: add Daily Deviations source

The endpoint takes only a date, so there is no pagination to write. A single
malformed result is skipped with a warning rather than failing the run,
since one bad row should not cost the user every other notification."
```

---

## Task 8: ntfy notifier

**Files:**
- Create: `script/src/dawatch/notifier.py`
- Create: `script/tests/test_notifier.py`

**Interfaces:**
- Consumes: `Deviation` (Task 3), `NotifyError` (Task 1)
- Produces:
  - `Notifier` protocol: `send(deviation: Deviation) -> None`
  - `NtfyNotifier(http: httpx.Client, base_url: str, topic: str)`
  - `ConsoleNotifier()` — prints, for `--dry-run` and local use
  - `RecordingNotifier()` — test double exposing `sent: list[Deviation]`

- [ ] **Step 1: Write the failing test `script/tests/test_notifier.py`**

```python
import httpx
import pytest
import respx

from dawatch.errors import NotifyError
from dawatch.models import Deviation
from dawatch.notifier import ConsoleNotifier, NtfyNotifier, RecordingNotifier

TOPIC_URL = "https://ntfy.sh/my-topic"

DEVIATION = Deviation.model_validate(
    {
        "deviationid": "ABC-123",
        "title": "Neon Alley",
        "url": "https://www.deviantart.com/artist/art/neon-alley",
        "author": {"username": "artist"},
        "preview": {"src": "https://images.invalid/preview.jpg"},
    }
)


@pytest.fixture
def notifier():
    with httpx.Client() as http:
        yield NtfyNotifier(http, "https://ntfy.sh", "my-topic")


@respx.mock
def test_posts_to_the_topic_url(notifier):
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))

    notifier.send(DEVIATION)

    assert route.called


@respx.mock
def test_sends_title_and_body(notifier):
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    notifier.send(DEVIATION)

    headers = route.calls.last.request.headers
    assert headers["X-Title"] == "Neon Alley"
    assert "artist" in route.calls.last.request.content.decode()


@respx.mock
def test_sets_click_and_attach_headers(notifier):
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    notifier.send(DEVIATION)

    headers = route.calls.last.request.headers
    assert headers["X-Click"] == "https://www.deviantart.com/artist/art/neon-alley"
    assert headers["X-Attach"] == "https://images.invalid/preview.jpg"


@respx.mock
def test_omits_optional_headers_when_data_is_absent(notifier):
    """ntfy rejects empty header values, so absent data means absent header."""
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    notifier.send(Deviation.model_validate({"deviationid": "X"}))

    headers = route.calls.last.request.headers
    assert "X-Click" not in headers
    assert "X-Attach" not in headers


@respx.mock
def test_encodes_non_ascii_titles(notifier):
    """ntfy headers are latin-1; a unicode title must not raise."""
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    notifier.send(Deviation.model_validate({"deviationid": "X", "title": "Sünset 日"}))

    assert route.called
    assert route.calls.last.request.headers["X-Title"].isascii()


@respx.mock
def test_strips_trailing_slash_from_base_url():
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    with httpx.Client() as http:
        NtfyNotifier(http, "https://ntfy.sh/", "my-topic").send(DEVIATION)

    assert route.called


@respx.mock
def test_server_error_raises_notify_error(notifier):
    respx.post(TOPIC_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(NotifyError) as exc_info:
        notifier.send(DEVIATION)

    assert "500" in str(exc_info.value)


@respx.mock
def test_transport_failure_raises_notify_error(notifier):
    respx.post(TOPIC_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(NotifyError):
        notifier.send(DEVIATION)


def test_console_notifier_prints(capsys):
    ConsoleNotifier().send(DEVIATION)

    out = capsys.readouterr().out
    assert "Neon Alley" in out
    assert "artist" in out


def test_recording_notifier_collects_sends():
    notifier = RecordingNotifier()

    notifier.send(DEVIATION)

    assert notifier.sent == [DEVIATION]


def test_recording_notifier_can_be_told_to_fail():
    notifier = RecordingNotifier(fail_ids={"ABC-123"})

    with pytest.raises(NotifyError):
        notifier.send(DEVIATION)

    assert notifier.sent == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_notifier.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.notifier'`

- [ ] **Step 3: Write `script/src/dawatch/notifier.py`**

```python
"""Notification delivery.

ntfy carries the message body as the request body and its metadata as
headers. HTTP headers are latin-1, so any non-ASCII text is escaped before
it goes into a header value.
"""

from typing import Protocol

import httpx
import structlog

from dawatch.errors import NotifyError
from dawatch.models import Deviation

log = structlog.get_logger(__name__)


def _header_safe(value: str) -> str:
    """Make a string safe for an HTTP header value.

    Non-ASCII characters are backslash-escaped rather than dropped, so a
    title in Japanese still tells the reader something.
    """
    return value.encode("ascii", "backslashreplace").decode("ascii")


class Notifier(Protocol):
    def send(self, deviation: Deviation) -> None: ...


class NtfyNotifier:
    """Publishes one notification per deviation to an ntfy topic."""

    def __init__(self, http: httpx.Client, base_url: str, topic: str) -> None:
        self._http = http
        self._url = f"{base_url.rstrip('/')}/{topic}"

    def send(self, deviation: Deviation) -> None:
        """Publish a single deviation.

        Raises:
            NotifyError: if the topic could not be reached or refused the
                message. The caller leaves the deviation unseen so the next
                run retries it.
        """
        headers = {
            "X-Title": _header_safe(deviation.title),
            "X-Tags": "art",
            "X-Priority": "default",
        }
        if deviation.url:
            headers["X-Click"] = deviation.url
        if deviation.image_url:
            headers["X-Attach"] = deviation.image_url

        body = f"New Daily Deviation by {deviation.author_name}"

        try:
            response = self._http.post(self._url, content=body.encode(), headers=headers)
        except httpx.HTTPError as exc:
            raise NotifyError(
                f"Could not reach ntfy for {deviation.deviationid}: {type(exc).__name__}"
            ) from exc

        if response.status_code != httpx.codes.OK:
            raise NotifyError(
                f"ntfy refused {deviation.deviationid} with HTTP {response.status_code}"
            )

        log.info("notify.sent", deviationid=deviation.deviationid, title=deviation.title)


class ConsoleNotifier:
    """Prints instead of sending. Used by --dry-run."""

    def send(self, deviation: Deviation) -> None:
        print(f"[dry-run] {deviation.title} by {deviation.author_name} -> {deviation.url or '-'}")


class RecordingNotifier:
    """Test double that records sends and can be told to fail for given ids."""

    def __init__(self, fail_ids: set[str] | None = None) -> None:
        self.sent: list[Deviation] = []
        self._fail_ids = fail_ids or set()

    def send(self, deviation: Deviation) -> None:
        if deviation.deviationid in self._fail_ids:
            raise NotifyError(f"forced failure for {deviation.deviationid}")
        self.sent.append(deviation)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_notifier.py -v --no-cov`
Expected: 11 passed

- [ ] **Step 5: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add script/ && git commit -m "feat: add ntfy notifier

Deviation metadata travels in ntfy's headers: title, click-through URL and
preview image. Header values are escaped to ASCII because HTTP headers are
latin-1 and DeviantArt titles are not. Optional headers are omitted rather
than sent empty, which ntfy rejects."
```

---

## Task 9: Metrics sinks

**Files:**
- Create: `script/src/dawatch/metrics.py`
- Create: `script/tests/test_metrics.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `MetricsSink` protocol: `record_fetched(count: int)`, `record_notified(count: int)`, `record_error(stage: str)`, `record_success(timestamp: float)`, `record_duration(seconds: float)`, `flush()`
  - `NullSink()` — records into attributes, used when metrics are disabled and in tests
  - `PushgatewaySink(gateway_url: str, job: str = "dawatch", push: PushFn = ...)`

- [ ] **Step 1: Write the failing test `script/tests/test_metrics.py`**

```python
from prometheus_client import CollectorRegistry

from dawatch.metrics import NullSink, PushgatewaySink


def test_null_sink_records_without_pushing():
    sink = NullSink()

    sink.record_fetched(5)
    sink.record_notified(2)
    sink.record_error("fetch")
    sink.record_error("notify")
    sink.record_duration(1.5)
    sink.record_success(1000.0)
    sink.flush()

    assert sink.fetched == 5
    assert sink.notified == 2
    assert sink.errors == {"fetch": 1, "notify": 1}
    assert sink.duration == 1.5
    assert sink.success_timestamp == 1000.0
    assert sink.flushed is True


def test_pushgateway_sink_pushes_on_flush():
    pushes: list[tuple[str, str, CollectorRegistry]] = []

    sink = PushgatewaySink(
        "http://gw:9091",
        push=lambda gateway, job, registry: pushes.append((gateway, job, registry)),
    )
    sink.record_fetched(3)
    sink.flush()

    assert len(pushes) == 1
    assert pushes[0][0] == "http://gw:9091"
    assert pushes[0][1] == "dawatch"


def test_pushgateway_sink_does_not_push_before_flush():
    pushes: list[object] = []

    sink = PushgatewaySink("http://gw:9091", push=lambda *args: pushes.append(args))
    sink.record_fetched(3)

    assert pushes == []


def test_pushgateway_sink_records_metric_values():
    sink = PushgatewaySink("http://gw:9091", push=lambda *args: None)

    sink.record_fetched(4)
    sink.record_notified(2)
    sink.record_error("notify")
    sink.record_success(1700.0)

    registry = sink.registry
    assert registry.get_sample_value("dawatch_deviations_fetched_total") == 4.0
    assert registry.get_sample_value("dawatch_notifications_sent_total") == 2.0
    assert (
        registry.get_sample_value("dawatch_errors_total", {"stage": "notify"}) == 1.0
    )
    assert registry.get_sample_value("dawatch_last_success_timestamp_seconds") == 1700.0


def test_a_failing_push_does_not_raise():
    """Metrics are diagnostics. A dead Pushgateway must not fail the run."""

    def explode(gateway: str, job: str, registry: CollectorRegistry) -> None:
        raise OSError("gateway unreachable")

    sink = PushgatewaySink("http://gw:9091", push=explode)
    sink.record_fetched(1)

    sink.flush()  # must not raise
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_metrics.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.metrics'`

- [ ] **Step 3: Write `script/src/dawatch/metrics.py`**

```python
"""Metrics.

A CronJob pod has usually exited before Prometheus would notice it, so it
cannot be scraped. Metrics are pushed to a Pushgateway instead, which exists
precisely for batch jobs.
"""

from collections.abc import Callable
from typing import Protocol

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import push_to_gateway as _push_to_gateway

log = structlog.get_logger(__name__)

PushFn = Callable[[str, str, CollectorRegistry], None]


class MetricsSink(Protocol):
    def record_fetched(self, count: int) -> None: ...

    def record_notified(self, count: int) -> None: ...

    def record_error(self, stage: str) -> None: ...

    def record_success(self, timestamp: float) -> None: ...

    def record_duration(self, seconds: float) -> None: ...

    def flush(self) -> None: ...


class NullSink:
    """Records nothing anywhere. Used when metrics are disabled, and in tests."""

    def __init__(self) -> None:
        self.fetched = 0
        self.notified = 0
        self.errors: dict[str, int] = {}
        self.duration: float | None = None
        self.success_timestamp: float | None = None
        self.flushed = False

    def record_fetched(self, count: int) -> None:
        self.fetched += count

    def record_notified(self, count: int) -> None:
        self.notified += count

    def record_error(self, stage: str) -> None:
        self.errors[stage] = self.errors.get(stage, 0) + 1

    def record_success(self, timestamp: float) -> None:
        self.success_timestamp = timestamp

    def record_duration(self, seconds: float) -> None:
        self.duration = seconds

    def flush(self) -> None:
        self.flushed = True


def _default_push(gateway: str, job: str, registry: CollectorRegistry) -> None:
    _push_to_gateway(gateway, job=job, registry=registry)


class PushgatewaySink:
    """Collects metrics for one run and pushes them at the end."""

    def __init__(
        self,
        gateway_url: str,
        job: str = "dawatch",
        push: PushFn = _default_push,
    ) -> None:
        self._gateway_url = gateway_url
        self._job = job
        self._push = push
        self.registry = CollectorRegistry()

        self._fetched = Counter(
            "dawatch_deviations_fetched_total",
            "Deviations returned by the feed",
            registry=self.registry,
        )
        self._notified = Counter(
            "dawatch_notifications_sent_total",
            "Notifications delivered successfully",
            registry=self.registry,
        )
        self._errors = Counter(
            "dawatch_errors_total",
            "Errors by pipeline stage",
            ["stage"],
            registry=self.registry,
        )
        self._last_success = Gauge(
            "dawatch_last_success_timestamp_seconds",
            "Unix timestamp of the last fully successful run",
            registry=self.registry,
        )
        self._duration = Histogram(
            "dawatch_run_duration_seconds",
            "Wall-clock duration of a run",
            registry=self.registry,
        )

    def record_fetched(self, count: int) -> None:
        self._fetched.inc(count)

    def record_notified(self, count: int) -> None:
        self._notified.inc(count)

    def record_error(self, stage: str) -> None:
        self._errors.labels(stage=stage).inc()

    def record_success(self, timestamp: float) -> None:
        self._last_success.set(timestamp)

    def record_duration(self, seconds: float) -> None:
        self._duration.observe(seconds)

    def flush(self) -> None:
        """Push to the gateway, swallowing failures.

        Metrics are diagnostics. An unreachable Pushgateway must never turn a
        successful notification run into a failed one.
        """
        try:
            self._push(self._gateway_url, self._job, self.registry)
            log.debug("metrics.pushed", gateway=self._gateway_url)
        except Exception as exc:  # noqa: BLE001 - deliberately non-fatal
            log.warning("metrics.push_failed", error=str(exc))
```

Note the `Counter` names: `prometheus_client` appends `_total` itself for
counters, so the constructor is given the name **with** `_total` already and
the library keeps it. If the assertion in Step 1 fails on a name, check the
sample name with `list(sink.registry.collect())` before changing the test.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_metrics.py -v --no-cov`
Expected: 5 passed

- [ ] **Step 5: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add script/ && git commit -m "feat: add metrics sinks

Pushgateway rather than scraping, because a CronJob pod has usually exited
before Prometheus would notice it. A failing push is logged and swallowed:
metrics are diagnostics, and a dead gateway must not turn a successful
notification run into a failed one."
```

---

## Task 10: WatchService orchestration

This is the centre of the test suite. Every behaviour the product promises is
asserted here, with no network, no clock, and no filesystem.

**Files:**
- Create: `script/src/dawatch/service.py`
- Create: `script/tests/test_service.py`

**Interfaces:**
- Consumes: `DeviationSource` (7), `SeenStore` (4), `Notifier` (8), `MetricsSink` (9)
- Produces:
  - `RunResult` dataclass: `fetched: int`, `new: int`, `notified: int`, `errors: int`, `seeded: bool`, and property `exit_code: int`
  - `WatchService(source, store, notifier, metrics, clock: Callable[[], float] = time.time)`
  - `WatchService.run(date: str | None = None, dry_run: bool = False, allow_seed: bool = True) -> RunResult`

- [ ] **Step 1: Write the failing test `script/tests/test_service.py`**

```python
import pytest

from dawatch.errors import FetchError
from dawatch.models import Deviation
from dawatch.metrics import NullSink
from dawatch.notifier import RecordingNotifier
from dawatch.service import WatchService
from dawatch.store import InMemoryStore


def make_deviation(deviationid: str) -> Deviation:
    return Deviation.model_validate(
        {
            "deviationid": deviationid,
            "title": f"Art {deviationid}",
            "author": {"username": "artist"},
            "url": f"https://example.invalid/{deviationid}",
        }
    )


class FakeSource:
    def __init__(self, deviations: list[Deviation], error: Exception | None = None) -> None:
        self._deviations = deviations
        self._error = error
        self.calls: list[str | None] = []

    def fetch(self, date: str | None = None) -> list[Deviation]:
        self.calls.append(date)
        if self._error is not None:
            raise self._error
        return list(self._deviations)


def build(deviations, *, store=None, notifier=None, source_error=None):
    store = store or InMemoryStore()
    notifier = notifier or RecordingNotifier()
    metrics = NullSink()
    source = FakeSource(deviations, source_error)
    service = WatchService(source, store, notifier, metrics, clock=lambda: 1000.0)
    return service, store, notifier, metrics, source


# --- seeding -------------------------------------------------------------


def test_empty_store_seeds_without_notifying():
    """A first deployment must not fire twenty notifications at once."""
    service, store, notifier, _, _ = build([make_deviation("A"), make_deviation("B")])

    result = service.run()

    assert result.seeded is True
    assert result.notified == 0
    assert notifier.sent == []
    assert store.has_seen("A") and store.has_seen("B")


def test_seeded_rows_are_not_marked_notified():
    service, store, _, _, _ = build([make_deviation("A")])

    service.run()

    assert store.notified_at("A") is None


def test_no_seed_notifies_everything_on_an_empty_store():
    service, _, notifier, _, _ = build([make_deviation("A")])

    result = service.run(allow_seed=False)

    assert result.seeded is False
    assert [d.deviationid for d in notifier.sent] == ["A"]


def test_a_non_empty_store_does_not_seed():
    store = InMemoryStore()
    store.mark_seen(make_deviation("OLD"), notified=True)
    service, _, notifier, _, _ = build([make_deviation("A")], store=store)

    result = service.run()

    assert result.seeded is False
    assert [d.deviationid for d in notifier.sent] == ["A"]


# --- the normal path -----------------------------------------------------


def seeded_store() -> InMemoryStore:
    store = InMemoryStore()
    store.mark_seen(make_deviation("SEED"), notified=True)
    return store


def test_notifies_only_unseen_deviations():
    store = seeded_store()
    store.mark_seen(make_deviation("A"), notified=True)
    service, _, notifier, _, _ = build(
        [make_deviation("A"), make_deviation("B")], store=store
    )

    result = service.run()

    assert [d.deviationid for d in notifier.sent] == ["B"]
    assert result.fetched == 2
    assert result.new == 1
    assert result.notified == 1


def test_notifies_nothing_when_everything_is_seen():
    store = seeded_store()
    store.mark_seen(make_deviation("A"), notified=True)
    service, _, notifier, _, _ = build([make_deviation("A")], store=store)

    result = service.run()

    assert notifier.sent == []
    assert result.new == 0
    assert result.exit_code == 0


def test_handles_an_empty_feed():
    service, _, notifier, _, _ = build([], store=seeded_store())

    result = service.run()

    assert notifier.sent == []
    assert result.fetched == 0
    assert result.exit_code == 0


def test_marks_seen_after_a_successful_send():
    store = seeded_store()
    service, _, _, _, _ = build([make_deviation("A")], store=store)

    service.run()

    assert store.has_seen("A")
    assert store.notified_at("A") is not None


def test_does_not_renotify_on_a_second_run():
    """The core promise: no deviation is ever notified twice."""
    store = seeded_store()
    notifier = RecordingNotifier()
    deviations = [make_deviation("A")]

    build(deviations, store=store, notifier=notifier)[0].run()
    build(deviations, store=store, notifier=notifier)[0].run()

    assert [d.deviationid for d in notifier.sent] == ["A"]


def test_forwards_the_date_to_the_source():
    service, _, _, _, source = build([], store=seeded_store())

    service.run(date="2026-08-01")

    assert source.calls == ["2026-08-01"]


# --- failure handling ----------------------------------------------------


def test_a_failed_notification_leaves_the_deviation_unseen():
    """The other half of the promise: nothing is silently dropped."""
    store = seeded_store()
    notifier = RecordingNotifier(fail_ids={"A"})
    service, _, _, _, _ = build([make_deviation("A")], store=store, notifier=notifier)

    result = service.run()

    assert store.has_seen("A") is False
    assert result.errors == 1
    assert result.exit_code == 1


def test_a_failed_notification_does_not_stop_later_ones():
    store = seeded_store()
    notifier = RecordingNotifier(fail_ids={"A"})
    service, _, _, _, _ = build(
        [make_deviation("A"), make_deviation("B")], store=store, notifier=notifier
    )

    result = service.run()

    assert [d.deviationid for d in notifier.sent] == ["B"]
    assert store.has_seen("B") is True
    assert result.notified == 1
    assert result.errors == 1


def test_a_failed_deviation_is_retried_next_run():
    store = seeded_store()
    deviations = [make_deviation("A")]

    failing = RecordingNotifier(fail_ids={"A"})
    build(deviations, store=store, notifier=failing)[0].run()

    working = RecordingNotifier()
    build(deviations, store=store, notifier=working)[0].run()

    assert [d.deviationid for d in working.sent] == ["A"]


def test_a_fetch_failure_propagates():
    service, _, _, _, _ = build([], store=seeded_store(), source_error=FetchError("down"))

    with pytest.raises(FetchError):
        service.run()


def test_a_fetch_failure_records_a_metric():
    store = seeded_store()
    metrics = NullSink()
    source = FakeSource([], FetchError("down"))
    service = WatchService(source, store, RecordingNotifier(), metrics, clock=lambda: 1000.0)

    with pytest.raises(FetchError):
        service.run()

    assert metrics.errors == {"fetch": 1}


# --- dry run -------------------------------------------------------------


def test_dry_run_notifies_nothing_and_writes_nothing():
    store = seeded_store()
    service, _, notifier, _, _ = build([make_deviation("A")], store=store)

    result = service.run(dry_run=True)

    assert notifier.sent == []
    assert store.has_seen("A") is False
    assert result.new == 1


def test_dry_run_does_not_seed_an_empty_store():
    store = InMemoryStore()
    service, _, _, _, _ = build([make_deviation("A")], store=store)

    service.run(dry_run=True)

    assert store.is_empty() is True


# --- metrics -------------------------------------------------------------


def test_records_metrics_for_a_successful_run():
    store = seeded_store()
    service, _, _, metrics, _ = build([make_deviation("A")], store=store)

    service.run()

    assert metrics.fetched == 1
    assert metrics.notified == 1
    assert metrics.success_timestamp == 1000.0
    assert metrics.duration is not None
    assert metrics.flushed is True


def test_does_not_record_success_when_a_notification_failed():
    """The staleness alert must not be reset by a partially failed run."""
    store = seeded_store()
    notifier = RecordingNotifier(fail_ids={"A"})
    service, _, _, metrics, _ = build([make_deviation("A")], store=store, notifier=notifier)

    service.run()

    assert metrics.success_timestamp is None
    assert metrics.errors == {"notify": 1}


def test_flushes_metrics_even_when_fetching_fails():
    store = seeded_store()
    metrics = NullSink()
    source = FakeSource([], FetchError("down"))
    service = WatchService(source, store, RecordingNotifier(), metrics, clock=lambda: 1000.0)

    with pytest.raises(FetchError):
        service.run()

    assert metrics.flushed is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_service.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.service'`

- [ ] **Step 3: Write `script/src/dawatch/service.py`**

```python
"""Orchestration.

This module depends only on the four protocols, never on httpx, sqlite3, or
the environment. That is what makes the behaviour above testable without a
network, a clock, or a filesystem.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from dawatch.errors import NotifyError
from dawatch.metrics import MetricsSink
from dawatch.models import Deviation
from dawatch.notifier import Notifier
from dawatch.sources import DeviationSource
from dawatch.store import SeenStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RunResult:
    fetched: int
    new: int
    notified: int
    errors: int
    seeded: bool

    @property
    def exit_code(self) -> int:
        """0 when everything worked, 1 when any notification failed."""
        return 1 if self.errors else 0


class WatchService:
    """Fetch the feed, notify what is new, remember what was notified."""

    def __init__(
        self,
        source: DeviationSource,
        store: SeenStore,
        notifier: Notifier,
        metrics: MetricsSink,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._store = store
        self._notifier = notifier
        self._metrics = metrics
        self._clock = clock

    def run(
        self,
        date: str | None = None,
        dry_run: bool = False,
        allow_seed: bool = True,
    ) -> RunResult:
        """Perform exactly one poll.

        Args:
            date: Feed day in YYYY-MM-DD. Defaults to today.
            dry_run: Report what would be sent without sending or writing.
            allow_seed: When the store is empty, record the feed as seen
                without notifying. Prevents a first deployment from firing a
                burst of notifications.

        Raises:
            FetchError: if the feed could not be retrieved.
        """
        started = self._clock()

        try:
            deviations = self._fetch(date)
            seeding = allow_seed and not dry_run and self._store.is_empty()

            new = [d for d in deviations if not self._store.has_seen(d.deviationid)]
            self._metrics.record_fetched(len(deviations))

            if seeding:
                self._seed(new)
                return self._finish(started, len(deviations), len(new), 0, 0, seeded=True)

            if dry_run:
                for deviation in new:
                    log.info(
                        "run.would_notify",
                        deviationid=deviation.deviationid,
                        title=deviation.title,
                    )
                return self._finish(started, len(deviations), len(new), 0, 0, seeded=False)

            notified, errors = self._notify_all(new)
            return self._finish(
                started, len(deviations), len(new), notified, errors, seeded=False
            )
        finally:
            self._metrics.record_duration(self._clock() - started)
            self._metrics.flush()

    def _fetch(self, date: str | None) -> list[Deviation]:
        try:
            return list(self._source.fetch(date))
        except Exception:
            self._metrics.record_error("fetch")
            raise

    def _seed(self, new: list[Deviation]) -> None:
        """Record deviations as seen without notifying."""
        for deviation in new:
            self._store.mark_seen(deviation, notified=False)
        log.info("run.seeded", count=len(new))

    def _notify_all(self, new: list[Deviation]) -> tuple[int, int]:
        """Notify each deviation, marking it seen only once delivery succeeds.

        The ordering is deliberate. Marking seen after a successful send gives
        at-least-once delivery: a crash between the two causes one duplicate
        next run. The reverse would give at-most-once, where the same crash
        loses the deviation permanently and silently.
        """
        notified = 0
        errors = 0

        for deviation in new:
            try:
                self._notifier.send(deviation)
            except NotifyError as exc:
                errors += 1
                self._metrics.record_error("notify")
                log.error(
                    "run.notify_failed",
                    deviationid=deviation.deviationid,
                    error=str(exc),
                )
                continue

            self._store.mark_seen(deviation, notified=True)
            notified += 1

        return notified, errors

    def _finish(
        self,
        started: float,
        fetched: int,
        new: int,
        notified: int,
        errors: int,
        seeded: bool,
    ) -> RunResult:
        result = RunResult(
            fetched=fetched, new=new, notified=notified, errors=errors, seeded=seeded
        )
        if errors == 0:
            # Only a clean run resets the staleness clock, so a run that is
            # half-failing does not look healthy to the alert.
            self._metrics.record_success(self._clock())

        self._metrics.record_notified(notified)
        log.info(
            "run.complete",
            fetched=fetched,
            new=new,
            notified=notified,
            errors=errors,
            seeded=seeded,
            duration_seconds=round(self._clock() - started, 3),
        )
        return result
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_service.py -v --no-cov`
Expected: 20 passed

- [ ] **Step 5: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add script/ && git commit -m "feat: add WatchService orchestration

Depends only on the four protocols, so every promise the product makes is
tested without a network, a clock, or a filesystem.

Two behaviours are load-bearing. A deviation is marked seen only after its
notification succeeds, giving at-least-once delivery. And an empty store is
seeded silently, so a first deployment does not fire twenty notifications
at once."
```

---

## Task 11: CLI and first end-to-end run

At the end of this task the watcher works on your laptop.

**Files:**
- Create: `script/src/dawatch/cli.py`, `script/src/dawatch/__main__.py`
- Create: `script/tests/test_cli.py`, `script/tests/test_integration.py`

**Interfaces:**
- Consumes: everything above
- Produces:
  - `main(argv: list[str] | None = None) -> int`
  - `build_parser() -> argparse.ArgumentParser`

- [ ] **Step 1: Write the failing test `script/tests/test_cli.py`**

```python
import httpx
import pytest
import respx

from dawatch.auth import TOKEN_URL
from dawatch.cli import main
from dawatch.client import API_BASE

FEED_URL = f"{API_BASE}/browse/dailydeviations"
PLACEBO_URL = f"{API_BASE}/placebo"
NTFY_URL = "https://ntfy.test/my-topic"

TOKEN_RESPONSE = {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}

FEED_RESPONSE = {
    "has_more": False,
    "results": [
        {
            "deviationid": "A",
            "title": "First",
            "author": {"username": "alice"},
            "url": "https://example.invalid/a",
        }
    ],
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVIANTART_CLIENT_ID", "cid")
    monkeypatch.setenv("DEVIANTART_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("DAWATCH_NTFY_TOPIC", "my-topic")
    monkeypatch.setenv("DAWATCH_NTFY_URL", "https://ntfy.test")
    monkeypatch.setenv("DAWATCH_DB_PATH", str(tmp_path / "dawatch.db"))
    monkeypatch.setenv("DAWATCH_ENV", "prod")
    monkeypatch.delenv("DAWATCH_PUSHGATEWAY_URL", raising=False)
    return tmp_path


def test_missing_config_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("DEVIANTART_CLIENT_ID", raising=False)
    monkeypatch.delenv("DEVIANTART_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("DAWATCH_NTFY_TOPIC", raising=False)

    assert main(["run"]) == 2
    assert "DEVIANTART_CLIENT_ID" in capsys.readouterr().err


@respx.mock
def test_first_run_seeds_and_exits_0(env):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    ntfy = respx.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))

    assert main(["run"]) == 0
    assert not ntfy.called, "a first run must seed, not notify"


@respx.mock
def test_second_run_notifies_and_exits_0(env):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    ntfy = respx.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))

    main(["seed"])
    second_feed = {
        "has_more": False,
        "results": [{"deviationid": "B", "title": "New", "author": {"username": "bob"}}],
    }
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=second_feed))

    assert main(["run"]) == 0
    assert ntfy.call_count == 1
    assert ntfy.calls.last.request.headers["X-Title"] == "New"


@respx.mock
def test_a_failed_notification_exits_1(env):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    respx.post(NTFY_URL).mock(return_value=httpx.Response(500))

    main(["seed"])
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"deviationid": "B", "author": {"username": "b"}}]},
        )
    )

    assert main(["run"]) == 1


@respx.mock
def test_a_fetch_failure_exits_1(env):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(503))

    assert main(["run"]) == 1


@respx.mock
def test_dry_run_sends_nothing(env):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    ntfy = respx.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))

    assert main(["run", "--dry-run"]) == 0
    assert not ntfy.called


@respx.mock
def test_no_seed_notifies_on_a_fresh_store(env):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    ntfy = respx.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))

    assert main(["run", "--no-seed"]) == 0
    assert ntfy.call_count == 1


@respx.mock
def test_date_is_forwarded_to_the_api(env):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    feed = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))

    main(["run", "--date", "2026-08-01"])

    assert feed.calls.last.request.url.params["date"] == "2026-08-01"


@respx.mock
def test_doctor_reports_healthy(env, capsys):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(PLACEBO_URL).mock(return_value=httpx.Response(200, json={"status": "success"}))

    assert main(["doctor"]) == 0

    out = capsys.readouterr().out
    assert "OK" in out


@respx.mock
def test_doctor_reports_bad_credentials(env, capsys):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "bad"}))

    assert main(["doctor"]) == 1
    assert "FAIL" in capsys.readouterr().out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd script && uv run pytest tests/test_cli.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'dawatch.cli'`

- [ ] **Step 3: Write `script/src/dawatch/cli.py`**

```python
"""Command line entry point.

``run`` performs exactly one poll and exits. The schedule lives in Kubernetes,
not in this process, so there is no loop, no signal handling, and no drift.
"""

import argparse
import sys
import uuid
from contextlib import ExitStack

import httpx
import structlog

from dawatch.auth import DeviantArtAuth
from dawatch.client import API_BASE, API_VERSION, DeviantArtClient
from dawatch.config import Settings
from dawatch.errors import ConfigError, DawatchError
from dawatch.logging import configure_logging
from dawatch.metrics import MetricsSink, NullSink, PushgatewaySink
from dawatch.notifier import ConsoleNotifier, Notifier, NtfyNotifier
from dawatch.service import WatchService
from dawatch.sources import DailyDeviationsSource
from dawatch.store import SqliteStore

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2

log = structlog.get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dawatch",
        description="Push new DeviantArt Daily Deviations to your phone.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Poll once and notify what is new")
    run.add_argument("--date", help="Feed day as YYYY-MM-DD (default: today)")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be sent without sending or writing",
    )
    run.add_argument(
        "--no-seed",
        action="store_true",
        help="Notify everything on an empty store instead of seeding silently",
    )

    seed = subparsers.add_parser(
        "seed", help="Record today's feed as seen without notifying"
    )
    seed.add_argument("--date", help="Feed day as YYYY-MM-DD (default: today)")

    subparsers.add_parser("doctor", help="Check configuration, token, store and gateway")

    return parser


def _build_metrics(settings: Settings) -> MetricsSink:
    if settings.pushgateway_url:
        return PushgatewaySink(settings.pushgateway_url)
    return NullSink()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = Settings.load()
    except ConfigError as exc:
        # Logging is not configured yet, so this goes straight to stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(settings)
    structlog.contextvars.bind_contextvars(run_id=str(uuid.uuid4()), command=args.command)

    try:
        with ExitStack() as stack:
            http = stack.enter_context(httpx.Client(timeout=settings.http_timeout))
            store = stack.enter_context(SqliteStore(settings.db_path))

            auth = DeviantArtAuth(
                http,
                settings.client_id.get_secret_value(),
                settings.client_secret.get_secret_value(),
                store,
            )

            if args.command == "doctor":
                return _doctor(settings, http, auth)

            client = DeviantArtClient(http, auth, max_retries=settings.max_retries)
            source = DailyDeviationsSource(client)

            dry_run = getattr(args, "dry_run", False)
            notifier: Notifier = (
                ConsoleNotifier()
                if dry_run
                else NtfyNotifier(http, settings.ntfy_url, settings.ntfy_topic)
            )

            service = WatchService(source, store, notifier, _build_metrics(settings))

            if args.command == "seed":
                deviations = source.fetch(args.date)
                for deviation in deviations:
                    store.mark_seen(deviation, notified=False)
                log.info("seed.complete", count=len(deviations))
                return EXIT_OK

            result = service.run(
                date=args.date,
                dry_run=dry_run,
                allow_seed=not args.no_seed,
            )
            return result.exit_code
    except DawatchError as exc:
        log.error("run.failed", error=str(exc), error_type=type(exc).__name__)
        return EXIT_FAILURE

    # Unreachable in practice. mypy types ExitStack.__exit__ as returning
    # bool, so it believes the with-block can swallow an exception and fall
    # through here; without this line --strict reports a missing return.
    return EXIT_FAILURE


def _doctor(settings: Settings, http: httpx.Client, auth: DeviantArtAuth) -> int:
    """Check every external dependency and report each one.

    Exists so an operator can answer 'is this deployment healthy' with one
    command instead of waiting to notice missing notifications.
    """
    failures = 0

    def report(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        status = "OK  " if ok else "FAIL"
        print(f"[{status}] {name}{f': {detail}' if detail else ''}")
        if not ok:
            failures += 1

    report("config", True, f"db={settings.db_path} topic={settings.ntfy_topic}")

    try:
        token = auth.token()
        report("token", True)
    except DawatchError as exc:
        report("token", False, str(exc))
        return EXIT_FAILURE

    try:
        response = http.get(
            f"{API_BASE}/placebo",
            headers={"Authorization": f"Bearer {token}", "dA-minor-version": API_VERSION},
        )
        report("api", response.status_code == httpx.codes.OK, f"HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        report("api", False, type(exc).__name__)

    if settings.pushgateway_url:
        try:
            response = http.get(settings.pushgateway_url, timeout=3.0)
            report("pushgateway", response.status_code < 500)
        except httpx.HTTPError as exc:
            report("pushgateway", False, type(exc).__name__)
    else:
        report("pushgateway", True, "disabled")

    return EXIT_OK if failures == 0 else EXIT_FAILURE
```

- [ ] **Step 4: Write `script/src/dawatch/__main__.py`**

```python
"""Allow ``python -m dawatch``."""

import sys

from dawatch.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd script && uv run pytest tests/test_cli.py -v --no-cov`
Expected: 10 passed

- [ ] **Step 6: Run the whole suite with coverage**

Run: `cd script && uv run pytest`
Expected: all tests pass and coverage is at or above 90%. If coverage falls
short, add tests for the uncovered lines the report names — do not lower the
threshold.

- [ ] **Step 7: Verify lint and types**

Run: `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: clean

- [ ] **Step 8: Real end-to-end check against the live API**

This is the moment the project first does its job. It needs real credentials
from https://www.deviantart.com/developers/apps and the ntfy app installed
on your phone, subscribed to your topic.

```bash
cd script
cp .env.example .env
# Edit .env: set DEVIANTART_CLIENT_ID, DEVIANTART_CLIENT_SECRET,
# and DAWATCH_NTFY_TOPIC to an unguessable string.

uv run dawatch doctor          # expect four [OK  ] lines
uv run dawatch run --dry-run   # expect a list of today's Daily Deviations
uv run dawatch run             # first run seeds silently, no phone buzz
uv run dawatch run --no-seed   # forces notifications; phone should buzz
```

If the phone does not buzz, check in this order: the topic in `.env` matches
the topic subscribed on the phone; `dawatch doctor` reports the API `[OK  ]`;
`DAWATCH_LOG_LEVEL=DEBUG uv run dawatch run --no-seed` shows a `notify.sent`
event.

- [ ] **Step 9: Commit**

```bash
git add script/ && git commit -m "feat: add CLI with run, seed and doctor commands

run performs exactly one poll and exits; the schedule belongs to Kubernetes,
so there is no loop and no drift to reason about. Exit codes separate a
configuration failure, which will never succeed on retry, from a transient
one, which the CronJob backoff should retry.

doctor checks credentials, the API via /placebo, the store and the gateway
in one command, so a deployment can be verified without waiting to notice
missing notifications."
```

---

## Task 12: Container image

**Files:**
- Create: `script/Dockerfile`, `script/.dockerignore`

**Interfaces:**
- Consumes: the `dawatch` entry point from Task 11
- Produces: image `dawatch:dev` running as UID 10001 with `/data` the only writable path

- [ ] **Step 1: Write `script/.dockerignore`**

```
.venv/
data/
.env
.env.*
!.env.example
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
tests/
```

- [ ] **Step 2: Write `script/Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies are installed before the source is copied, so an edit to the
# application does not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm AS runtime

# A fixed high UID keeps the image compatible with a restrictive
# PodSecurityContext and makes the PVC's ownership predictable.
RUN groupadd --gid 10001 dawatch \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin dawatch \
    && mkdir -p /data \
    && chown 10001:10001 /data

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DAWATCH_DB_PATH=/data/dawatch.db \
    DAWATCH_ENV=prod

USER 10001:10001
WORKDIR /app
VOLUME ["/data"]

ENTRYPOINT ["dawatch"]
CMD ["run"]
```

- [ ] **Step 3: Build the image**

Run: `docker build -t dawatch:dev script/`
Expected: build succeeds.

- [ ] **Step 4: Verify the entry point and the non-root user**

```bash
docker run --rm dawatch:dev --help
docker run --rm --entrypoint id dawatch:dev
```

Expected: the help text lists `run`, `seed`, `doctor`; `id` reports
`uid=10001 gid=10001`.

- [ ] **Step 5: Verify a config failure still exits 2 in the container**

Run: `docker run --rm dawatch:dev run; echo "exit=$?"`
Expected: `Configuration error: ...` on stderr and `exit=2`.

- [ ] **Step 6: Verify a real run works in the container**

```bash
mkdir -p "$PWD/script/data"
docker run --rm \
  --env-file script/.env \
  -e DAWATCH_DB_PATH=/data/dawatch.db \
  -v "$PWD/script/data:/data" \
  --user 10001:10001 \
  dawatch:dev doctor
```

Expected: `[OK  ]` lines. If the run fails with a permission error on
`/data`, the host directory is owned by your user rather than 10001 — run
`sudo chown 10001:10001 script/data` and retry. This is the same ownership
issue the PVC will have, so it is worth understanding now.

- [ ] **Step 7: Commit**

```bash
git add script/Dockerfile script/.dockerignore && git commit -m "feat: add container image

Multi-stage build with uv. Dependencies install before the source is copied,
so editing the application does not invalidate the dependency layer.

Runs as a fixed UID 10001 so the image works under a restrictive pod
security context and the PVC's ownership is predictable. /data is the only
path the process needs to write."
```

---

## Task 13: Kubernetes manifests

**Files:**
- Create: `k8s/base/{namespace,configmap,pvc,cronjob,kustomization}.yaml`
- Create: `k8s/overlays/local/{kustomization.yaml,patch-cronjob.yaml}`
- Create: `k8s/overlays/prod/{kustomization.yaml,patch-cronjob.yaml}`
- Create: `k8s/secret.example.yaml`
- Create: `Makefile` (repo root)

**Interfaces:**
- Consumes: image `dawatch:dev` from Task 12
- Produces: a `dawatch` CronJob in namespace `dawatch`, reading env from ConfigMap `dawatch-config` and Secret `dawatch-secrets`, writing to PVC `dawatch-data`

- [ ] **Step 1: Install kind and kubectl**

Neither is currently on this machine.

```bash
# kubectl
curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -m 0755 /tmp/kubectl /usr/local/bin/kubectl

# kind
curl -fsSLo /tmp/kind https://kind.sigs.k8s.io/dl/v0.30.0/kind-linux-amd64
sudo install -m 0755 /tmp/kind /usr/local/bin/kind

kubectl version --client
kind version
```

Expected: both print a version. Kustomize needs no install — `kubectl -k` has
it built in.

- [ ] **Step 2: Write `k8s/base/namespace.yaml`**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: dawatch
  labels:
    app.kubernetes.io/name: dawatch
```

- [ ] **Step 3: Write `k8s/base/configmap.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: dawatch-config
  namespace: dawatch
data:
  DAWATCH_NTFY_URL: "https://ntfy.sh"
  DAWATCH_DB_PATH: "/data/dawatch.db"
  DAWATCH_ENV: "prod"
  DAWATCH_LOG_LEVEL: "INFO"
  DAWATCH_HTTP_TIMEOUT: "10.0"
  DAWATCH_MAX_RETRIES: "3"
  DAWATCH_PUSHGATEWAY_URL: "http://pushgateway.dawatch.svc.cluster.local:9091"
```

- [ ] **Step 4: Write `k8s/secret.example.yaml`**

```yaml
# Copy to k8s/secret.yaml, fill in real values, and apply it separately.
# k8s/secret.yaml is gitignored. Never commit real credentials.
#
#   cp k8s/secret.example.yaml k8s/secret.yaml
#   $EDITOR k8s/secret.yaml
#   kubectl apply -f k8s/secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: dawatch-secrets
  namespace: dawatch
type: Opaque
stringData:
  DEVIANTART_CLIENT_ID: "replace-me"
  DEVIANTART_CLIENT_SECRET: "replace-me"
  DAWATCH_NTFY_TOPIC: "replace-me-with-something-unguessable"
```

- [ ] **Step 5: Write `k8s/base/pvc.yaml`**

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: dawatch-data
  namespace: dawatch
spec:
  # ReadWriteOnce is correct here: exactly one pod writes the SQLite file at
  # a time, enforced by the CronJob's concurrencyPolicy: Forbid.
  #
  # The backing StorageClass must be local-path or block storage. SQLite's
  # advisory locking is unreliable over NFS and can corrupt the database.
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
```

- [ ] **Step 6: Write `k8s/base/cronjob.yaml`**

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: dawatch
  namespace: dawatch
  labels:
    app.kubernetes.io/name: dawatch
spec:
  schedule: "0 9 * * *"
  timeZone: "Etc/UTC"

  # Exactly one writer for the SQLite file at any moment.
  concurrencyPolicy: Forbid

  # If the controller was down at the scheduled minute, skip the run rather
  # than start a stampede of catch-up jobs.
  startingDeadlineSeconds: 300

  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3

  jobTemplate:
    spec:
      # Retries transient failures (exit 1). A configuration failure exits 2
      # and is equally retried, so keep this low.
      backoffLimit: 2
      ttlSecondsAfterFinished: 3600
      template:
        metadata:
          labels:
            app.kubernetes.io/name: dawatch
        spec:
          restartPolicy: Never
          securityContext:
            runAsNonRoot: true
            runAsUser: 10001
            runAsGroup: 10001
            fsGroup: 10001
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: dawatch
              image: dawatch:dev
              args: ["run"]
              envFrom:
                - configMapRef:
                    name: dawatch-config
                - secretRef:
                    name: dawatch-secrets
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities:
                  drop: ["ALL"]
              resources:
                requests:
                  cpu: 25m
                  memory: 64Mi
                limits:
                  cpu: 500m
                  memory: 256Mi
              volumeMounts:
                - name: data
                  mountPath: /data
                - name: tmp
                  mountPath: /tmp
          volumes:
            - name: data
              persistentVolumeClaim:
                claimName: dawatch-data
            # readOnlyRootFilesystem is on, so anything writing to /tmp needs
            # an explicit emptyDir.
            - name: tmp
              emptyDir: {}
```

- [ ] **Step 7: Write `k8s/base/kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: dawatch

resources:
  - namespace.yaml
  - configmap.yaml
  - pvc.yaml
  - cronjob.yaml

labels:
  - includeSelectors: false
    pairs:
      app.kubernetes.io/name: dawatch
      app.kubernetes.io/part-of: dawatch
```

- [ ] **Step 8: Write `k8s/overlays/local/patch-cronjob.yaml`**

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: dawatch
spec:
  # Frequent enough to demo without waiting until tomorrow.
  schedule: "*/5 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: dawatch
              # The image is side-loaded into kind, never pulled.
              imagePullPolicy: IfNotPresent
              env:
                - name: DAWATCH_LOG_LEVEL
                  value: "DEBUG"
                - name: DAWATCH_ENV
                  value: "dev"
```

- [ ] **Step 9: Write `k8s/overlays/local/kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: dawatch

resources:
  - ../../base

images:
  - name: dawatch
    newTag: dev

patches:
  - path: patch-cronjob.yaml
  # kind ships a local-path provisioner under the StorageClass name
  # "standard". Local-path is exactly what SQLite needs.
  - target:
      kind: PersistentVolumeClaim
      name: dawatch-data
    patch: |-
      - op: add
        path: /spec/storageClassName
        value: standard
```

A Kustomization takes exactly one `patches:` key, so both patches are entries
in the same list: the CronJob as a `path:` entry, the PVC as a
`target:`/`patch:` entry.

- [ ] **Step 10: Write `k8s/overlays/prod/patch-cronjob.yaml`**

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: dawatch
spec:
  schedule: "0 9 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: dawatch
              imagePullPolicy: IfNotPresent
```

- [ ] **Step 11: Write `k8s/overlays/prod/kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: dawatch

resources:
  - ../../base

images:
  # Pin by digest in a real deployment: a mutable tag makes a rollback
  # ambiguous and lets an image change under you between runs.
  - name: dawatch
    newTag: "0.1.0"

patches:
  - path: patch-cronjob.yaml
```

- [ ] **Step 12: Add `k8s/secret.yaml` to `.gitignore`**

Confirm the entry from the initial commit is present:

Run: `grep -n 'k8s/secret.yaml' .gitignore`
Expected: one match. If absent, append `k8s/secret.yaml` to `.gitignore`.

- [ ] **Step 13: Write the root `Makefile`**

```makefile
# Repo-level orchestration. `make demo` is the one-command path from a clean
# checkout to a running CronJob.

CLUSTER   ?= dawatch
IMAGE     ?= dawatch
TAG       ?= dev
NAMESPACE ?= dawatch

.PHONY: help test lint image kind-up kind-down load deploy secret trigger logs demo clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

test: ## Run the Python test suite
	cd script && uv run pytest

lint: ## Lint and type-check
	cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy

image: ## Build the container image
	docker build -t $(IMAGE):$(TAG) script/

kind-up: ## Create the local cluster
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --name $(CLUSTER)

kind-down: ## Delete the local cluster
	kind delete cluster --name $(CLUSTER)

load: image ## Side-load the image into the cluster
	kind load docker-image $(IMAGE):$(TAG) --name $(CLUSTER)

secret: ## Apply k8s/secret.yaml (create it from secret.example.yaml first)
	@test -f k8s/secret.yaml || { \
		echo "k8s/secret.yaml is missing."; \
		echo "  cp k8s/secret.example.yaml k8s/secret.yaml && \$$EDITOR k8s/secret.yaml"; \
		exit 1; }
	kubectl apply -f k8s/secret.yaml

deploy: ## Apply the local overlay
	kubectl apply -k k8s/overlays/local

trigger: ## Run the CronJob immediately instead of waiting for the schedule
	kubectl -n $(NAMESPACE) delete job dawatch-manual --ignore-not-found
	kubectl -n $(NAMESPACE) create job dawatch-manual --from=cronjob/dawatch
	kubectl -n $(NAMESPACE) wait --for=condition=complete job/dawatch-manual --timeout=120s

logs: ## Show logs from the most recent run
	kubectl -n $(NAMESPACE) logs -l app.kubernetes.io/name=dawatch --tail=100

demo: kind-up load deploy secret trigger logs ## Clean checkout to a completed run

clean: ## Remove local build and test artefacts
	rm -rf script/.pytest_cache script/.mypy_cache script/.ruff_cache script/data
	find script -name __pycache__ -type d -prune -exec rm -rf {} +
```

- [ ] **Step 14: Verify the manifests render**

```bash
kubectl kustomize k8s/overlays/local
kubectl kustomize k8s/overlays/prod
```

Expected: both render valid YAML. The `patches` duplication noted in Step 9
will surface here — merge the two lists and re-run until both render.

- [ ] **Step 15: Bring up the cluster and deploy**

```bash
make kind-up
make load
make deploy
cp k8s/secret.example.yaml k8s/secret.yaml
# Edit k8s/secret.yaml with your real credentials and ntfy topic.
make secret
```

Expected: `kubectl -n dawatch get cronjob,pvc` shows the CronJob and a
`Bound` PVC.

- [ ] **Step 16: Trigger a run and read the logs**

```bash
make trigger
make logs
```

Expected: the job reaches `Complete`, and the logs contain a JSON line with
`"event": "run.seeded"` on the first run. Run `make trigger && make logs`
again; the second run logs `"event": "run.complete"` and your phone buzzes if
the feed has anything the store has not seen.

If the pod fails with a permission error writing `/data`, confirm `fsGroup:
10001` is present in the pod security context — that is what makes kind's
local-path volume writable by the non-root user.

- [ ] **Step 17: Commit**

```bash
git add k8s/ Makefile .gitignore && git commit -m "feat: deploy as a Kubernetes CronJob

Kustomize base with local and prod overlays differing in image tag,
schedule and StorageClass.

concurrencyPolicy: Forbid pairs with the ReadWriteOnce claim to guarantee a
single SQLite writer. startingDeadlineSeconds skips a missed run rather than
starting a catch-up stampede. The pod runs non-root with a read-only root
filesystem, so /data and /tmp are the only writable mounts.

make demo takes a clean checkout to a completed run."
```

---

## Task 14: Observability stack

**Files:**
- Create: `k8s/observability/{pushgateway,prometheus,grafana,kustomization}.yaml`
- Create: `k8s/observability/dashboard.json`

**Interfaces:**
- Consumes: `DAWATCH_PUSHGATEWAY_URL` from the ConfigMap in Task 13
- Produces: Services `pushgateway:9091`, `prometheus:9090`, `grafana:3000` in namespace `dawatch`

- [ ] **Step 1: Write `k8s/observability/pushgateway.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pushgateway
  namespace: dawatch
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: pushgateway
  template:
    metadata:
      labels:
        app.kubernetes.io/name: pushgateway
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: pushgateway
          image: prom/pushgateway:v1.10.0
          args:
            # Metrics survive a Pushgateway restart, so a dashboard does not
            # go blank because the gateway was rescheduled.
            - --persistence.file=/data/pushgateway.store
            - --persistence.interval=1m
          ports:
            - name: http
              containerPort: 9091
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 200m
              memory: 128Mi
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: pushgateway
  namespace: dawatch
spec:
  selector:
    app.kubernetes.io/name: pushgateway
  ports:
    - name: http
      port: 9091
      targetPort: http
```

- [ ] **Step 2: Write `k8s/observability/prometheus.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-config
  namespace: dawatch
data:
  prometheus.yml: |
    global:
      scrape_interval: 30s
      evaluation_interval: 30s

    rule_files:
      - /etc/prometheus/rules/*.yml

    scrape_configs:
      - job_name: pushgateway
        # Without this, Prometheus overwrites the job label the CronJob
        # pushed with the scrape job's own name.
        honor_labels: true
        static_configs:
          - targets: ["pushgateway.dawatch.svc.cluster.local:9091"]
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-rules
  namespace: dawatch
data:
  dawatch.yml: |
    groups:
      - name: dawatch
        rules:
          - alert: DawatchStale
            # The failure that matters: a job that quietly stops running
            # produces no errors and no notifications, and is otherwise
            # invisible. Staleness is the only signal that catches it.
            expr: time() - dawatch_last_success_timestamp_seconds > 172800
            for: 10m
            labels:
              severity: warning
            annotations:
              summary: "dawatch has not completed a clean run in over 48 hours"

          - alert: DawatchErrors
            expr: increase(dawatch_errors_total[1h]) > 0
            for: 5m
            labels:
              severity: info
            annotations:
              summary: "dawatch reported errors in the last hour"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: prometheus
  namespace: dawatch
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: prometheus
  template:
    metadata:
      labels:
        app.kubernetes.io/name: prometheus
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        fsGroup: 65534
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: prometheus
          image: prom/prometheus:v3.1.0
          args:
            - --config.file=/etc/prometheus/prometheus.yml
            - --storage.tsdb.path=/prometheus
            - --storage.tsdb.retention.time=15d
          ports:
            - name: http
              containerPort: 9090
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 50m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 1Gi
          volumeMounts:
            - name: config
              mountPath: /etc/prometheus/prometheus.yml
              subPath: prometheus.yml
            - name: rules
              mountPath: /etc/prometheus/rules
            - name: data
              mountPath: /prometheus
      volumes:
        - name: config
          configMap:
            name: prometheus-config
        - name: rules
          configMap:
            name: prometheus-rules
        - name: data
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: prometheus
  namespace: dawatch
spec:
  selector:
    app.kubernetes.io/name: prometheus
  ports:
    - name: http
      port: 9090
      targetPort: http
```

- [ ] **Step 3: Write `k8s/observability/dashboard.json`**

```json
{
  "title": "DeviantArt Watcher",
  "uid": "dawatch",
  "timezone": "browser",
  "refresh": "30s",
  "time": { "from": "now-24h", "to": "now" },
  "panels": [
    {
      "id": 1,
      "type": "stat",
      "title": "Time since last clean run",
      "gridPos": { "h": 5, "w": 6, "x": 0, "y": 0 },
      "fieldConfig": {
        "defaults": {
          "unit": "s",
          "thresholds": {
            "mode": "absolute",
            "steps": [
              { "color": "green", "value": null },
              { "color": "orange", "value": 90000 },
              { "color": "red", "value": 172800 }
            ]
          }
        }
      },
      "targets": [
        {
          "expr": "time() - dawatch_last_success_timestamp_seconds",
          "refId": "A"
        }
      ]
    },
    {
      "id": 2,
      "type": "stat",
      "title": "Notifications sent (24h)",
      "gridPos": { "h": 5, "w": 6, "x": 6, "y": 0 },
      "targets": [
        { "expr": "increase(dawatch_notifications_sent_total[24h])", "refId": "A" }
      ]
    },
    {
      "id": 3,
      "type": "timeseries",
      "title": "Deviations fetched vs notified",
      "gridPos": { "h": 9, "w": 12, "x": 12, "y": 0 },
      "targets": [
        {
          "expr": "increase(dawatch_deviations_fetched_total[1h])",
          "legendFormat": "fetched",
          "refId": "A"
        },
        {
          "expr": "increase(dawatch_notifications_sent_total[1h])",
          "legendFormat": "notified",
          "refId": "B"
        }
      ]
    },
    {
      "id": 4,
      "type": "timeseries",
      "title": "Errors by stage",
      "gridPos": { "h": 9, "w": 12, "x": 0, "y": 5 },
      "targets": [
        {
          "expr": "increase(dawatch_errors_total[1h])",
          "legendFormat": "{{stage}}",
          "refId": "A"
        }
      ]
    }
  ]
}
```

- [ ] **Step 4: Write `k8s/observability/grafana.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-provisioning
  namespace: dawatch
data:
  datasources.yaml: |
    apiVersion: 1
    datasources:
      - name: Prometheus
        type: prometheus
        access: proxy
        url: http://prometheus.dawatch.svc.cluster.local:9090
        isDefault: true
  dashboards.yaml: |
    apiVersion: 1
    providers:
      - name: dawatch
        orgId: 1
        folder: ""
        type: file
        disableDeletion: false
        options:
          path: /var/lib/grafana/dashboards
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: grafana
  namespace: dawatch
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: grafana
  template:
    metadata:
      labels:
        app.kubernetes.io/name: grafana
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 472
        fsGroup: 472
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: grafana
          image: grafana/grafana:11.5.1
          env:
            # Anonymous viewer access: this is a local demo cluster, and a
            # login wall would only get in a reviewer's way. Never do this
            # on a cluster reachable from anywhere else.
            - name: GF_AUTH_ANONYMOUS_ENABLED
              value: "true"
            - name: GF_AUTH_ANONYMOUS_ORG_ROLE
              value: "Viewer"
            - name: GF_SECURITY_ADMIN_PASSWORD
              value: "admin"
            - name: GF_ANALYTICS_REPORTING_ENABLED
              value: "false"
          ports:
            - name: http
              containerPort: 3000
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
          volumeMounts:
            - name: provisioning-datasources
              mountPath: /etc/grafana/provisioning/datasources
            - name: provisioning-dashboards
              mountPath: /etc/grafana/provisioning/dashboards
            - name: dashboards
              mountPath: /var/lib/grafana/dashboards
      volumes:
        - name: provisioning-datasources
          configMap:
            name: grafana-provisioning
            items:
              - key: datasources.yaml
                path: datasources.yaml
        - name: provisioning-dashboards
          configMap:
            name: grafana-provisioning
            items:
              - key: dashboards.yaml
                path: dashboards.yaml
        - name: dashboards
          configMap:
            name: grafana-dashboards
---
apiVersion: v1
kind: Service
metadata:
  name: grafana
  namespace: dawatch
spec:
  selector:
    app.kubernetes.io/name: grafana
  ports:
    - name: http
      port: 3000
      targetPort: http
```

- [ ] **Step 5: Write `k8s/observability/kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: dawatch

resources:
  - pushgateway.yaml
  - prometheus.yaml
  - grafana.yaml

configMapGenerator:
  # The dashboard is version-controlled JSON rather than something clicked
  # together in the UI, so it survives a cluster rebuild.
  - name: grafana-dashboards
    files:
      - dawatch.json=dashboard.json

generatorOptions:
  disableNameSuffixHash: true
```

- [ ] **Step 6: Add the observability stack to the local overlay**

Edit `k8s/overlays/local/kustomization.yaml` and add `../../observability` to
its `resources` list:

```yaml
resources:
  - ../../base
  - ../../observability
```

- [ ] **Step 7: Add Makefile targets for the dashboards**

Append to the root `Makefile`:

```makefile
.PHONY: grafana prometheus

grafana: ## Port-forward Grafana to http://localhost:3000
	@echo "Grafana: http://localhost:3000 (anonymous viewer access)"
	kubectl -n $(NAMESPACE) port-forward svc/grafana 3000:3000

prometheus: ## Port-forward Prometheus to http://localhost:9090
	@echo "Prometheus: http://localhost:9090"
	kubectl -n $(NAMESPACE) port-forward svc/prometheus 9090:9090
```

- [ ] **Step 8: Deploy and verify the metrics arrive**

```bash
make deploy
kubectl -n dawatch rollout status deploy/pushgateway deploy/prometheus deploy/grafana
make trigger
kubectl -n dawatch run curl --rm -it --restart=Never --image=curlimages/curl:8.11.1 -- \
  curl -s http://pushgateway.dawatch.svc.cluster.local:9091/metrics | grep dawatch_
```

Expected: `dawatch_deviations_fetched_total`, `dawatch_notifications_sent_total`
and `dawatch_last_success_timestamp_seconds` all appear.

- [ ] **Step 9: Verify the dashboard renders**

Run `make grafana`, open http://localhost:3000, and open the "DeviantArt
Watcher" dashboard.

Expected: four panels, with "Time since last clean run" showing a small number
of seconds right after `make trigger`.

- [ ] **Step 10: Commit**

```bash
git add k8s/ Makefile && git commit -m "feat: add Prometheus, Pushgateway and Grafana

honor_labels is set on the scrape config, or Prometheus would overwrite the
job label the CronJob pushed with the scrape job's own name.

The alert that matters is staleness on dawatch_last_success_timestamp_seconds:
a job that quietly stops running produces no errors and no notifications, and
nothing else would catch it. The dashboard is version-controlled JSON rather
than something clicked together in the UI, so it survives a cluster rebuild."
```

---

## Task 15: CI and documentation

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `README.md`

**Interfaces:**
- Consumes: everything above
- Produces: a CI pipeline gating lint, types, unit tests, and a real in-cluster run

- [ ] **Step 1: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  quality:
    name: Lint, types and tests (py${{ matrix.python-version }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.13", "3.14"]
    defaults:
      run:
        working-directory: script
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
          cache-dependency-glob: script/uv.lock

      - run: uv python install ${{ matrix.python-version }}

      - name: Install dependencies
        run: uv sync --all-groups --frozen

      - name: Lint
        run: uv run ruff check --output-format=github .

      - name: Check formatting
        run: uv run ruff format --check .

      - name: Type check
        run: uv run mypy

      - name: Test
        run: uv run pytest

  manifests:
    name: Render Kubernetes manifests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Render every overlay
        run: |
          kubectl kustomize k8s/overlays/local > /dev/null
          kubectl kustomize k8s/overlays/prod > /dev/null

  cluster:
    name: End-to-end in a real cluster
    runs-on: ubuntu-latest
    needs: [quality, manifests]
    steps:
      - uses: actions/checkout@v4

      - name: Build the image
        run: docker build -t dawatch:dev script/

      - name: Create a kind cluster
        uses: helm/kind-action@v1
        with:
          cluster_name: dawatch

      - name: Load the image
        run: kind load docker-image dawatch:dev --name dawatch

      - name: Deploy
        run: kubectl apply -k k8s/overlays/local

      - name: Create a placeholder secret
        # The job must reach the DeviantArt API and fail there, not fail
        # earlier on missing configuration. That is what proves the wiring
        # from Secret to container environment actually works.
        run: |
          kubectl -n dawatch create secret generic dawatch-secrets \
            --from-literal=DEVIANTART_CLIENT_ID=ci-placeholder \
            --from-literal=DEVIANTART_CLIENT_SECRET=ci-placeholder \
            --from-literal=DAWATCH_NTFY_TOPIC=ci-placeholder

      - name: Wait for the PVC to bind
        run: kubectl -n dawatch wait --for=jsonpath='{.status.phase}'=Bound pvc/dawatch-data --timeout=60s

      - name: Trigger a run
        run: kubectl -n dawatch create job ci-run --from=cronjob/dawatch

      - name: Wait for the pod to finish
        run: |
          kubectl -n dawatch wait --for=condition=complete job/ci-run --timeout=120s \
            || kubectl -n dawatch wait --for=condition=failed job/ci-run --timeout=10s

      - name: Assert the container ran and reported an auth failure
        # Exit 1 with an auth error is the correct outcome for placeholder
        # credentials. Exit 2 would mean the Secret never reached the pod.
        run: |
          kubectl -n dawatch logs job/ci-run | tee /tmp/logs
          grep -q 'run.failed' /tmp/logs
          grep -q 'AuthError' /tmp/logs

      - name: Dump diagnostics on failure
        if: failure()
        run: |
          kubectl -n dawatch get all,pvc
          kubectl -n dawatch describe job/ci-run || true
          kubectl -n dawatch logs job/ci-run --tail=200 || true
```

- [ ] **Step 2: Run the quality job's commands locally to confirm they pass**

```bash
cd script
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Expected: all clean. Fix anything that fails before pushing — a red first CI
run on a portfolio repository is worth avoiding.

- [ ] **Step 3: Write `README.md`**

````markdown
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
make demo                                     # cluster, image, deploy, run, logs
make grafana                                  # http://localhost:3000
```

`make help` lists every target.

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

## Operational notes

The PVC must be backed by local-path or block storage. SQLite's advisory
locking is unreliable over NFS and can corrupt the database. The CronJob sets
`concurrencyPolicy: Forbid`, so exactly one process holds the file at a time.

The alert to wire up is staleness:

```
time() - dawatch_last_success_timestamp_seconds > 172800
```

A job that stops running produces no errors and no notifications. Nothing
else catches it.

## Development

```bash
make test    # pytest with coverage
make lint    # ruff + mypy --strict
make image   # build the container
```

CI runs lint, types, and tests on Python 3.13 and 3.14, renders every
Kustomize overlay, then builds the image and runs it in a real kind cluster.

## Not implemented

The "artists you watch" feed needs the OAuth2 authorization code flow — user
consent in a browser and refresh-token storage — rather than the client
credentials grant used here. It would arrive as a second `DeviationSource`
without any change to the orchestration layer.
````

- [ ] **Step 4: Commit**

```bash
git add .github/ README.md && git commit -m "ci: add pipeline and project documentation

CI gates lint, types and tests on 3.13 and 3.14, renders every overlay, then
builds the image and runs it in a real kind cluster.

The cluster job asserts the container fails with AuthError rather than a
config error: exit 2 would mean the Secret never reached the pod, so this
proves the Secret-to-environment wiring works even without real credentials."
```

- [ ] **Step 5: Push and confirm CI is green**

```bash
git push -u origin main
gh run watch
```

Expected: all three jobs pass. If `cluster` fails, read its diagnostics step —
it dumps the job description and pod logs.

---

## Verification

The project is complete when all of the following hold:

- [ ] `cd script && uv run pytest` passes with coverage at or above 90%
- [ ] `cd script && uv run ruff check . && uv run ruff format --check . && uv run mypy` is clean
- [ ] `uv run dawatch doctor` reports `[OK  ]` for config, token, api and pushgateway
- [ ] `uv run dawatch run --no-seed` makes the phone buzz
- [ ] A second `uv run dawatch run` sends nothing, proving deduplication
- [ ] `make demo` takes a clean checkout to a completed Job
- [ ] `make grafana` shows four populated panels
- [ ] CI is green on GitHub

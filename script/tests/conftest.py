"""Shared test fixtures."""

from collections.abc import Generator
from pathlib import Path

import pytest
import structlog


@pytest.fixture(autouse=True)
def isolate_from_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test from a clean directory.

    Settings reads a relative ``.env``. Without this, a developer's real
    ``script/.env`` would supply values for any field a test does not set,
    so a local dev file could fail tests that pass in CI.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def reset_structlog() -> Generator[None]:
    """Reset structlog to a sane default after each test.

    test_logging.py's test_configure_logging_emits_json_in_prod calls
    configure_logging(), which sets up structlog to write to sys.stdout.
    After that test, structlog retains that configuration. If stdout is
    closed (as it is after pytest's output capture ends), subsequent
    log calls fail with "I/O operation on closed file".
    """
    yield
    structlog.reset_defaults()

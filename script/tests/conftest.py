"""Shared test fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_from_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test from a clean directory.

    Settings reads a relative ``.env``. Without this, a developer's real
    ``script/.env`` would supply values for any field a test does not set,
    so a local dev file could fail tests that pass in CI.
    """
    monkeypatch.chdir(tmp_path)

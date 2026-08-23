"""End-to-end integration checks for the wired-up CLI.

test_cli.py exercises argument parsing and exit codes against the full stack
under respx. This module checks two things that file does not:

- that both real entry points work: the console script (``dawatch``, which
  every test_cli.py test already exercises indirectly by calling ``main()``)
  and ``python -m dawatch``, which only ``__main__.py``'s own guard covers.
- that ``main()``'s top-level ``except DawatchError`` does not just swallow a
  failure silently. Per this project's standard for catch-and-continue error
  handling, a test must assert both that the error did not propagate *and*
  that it was logged with enough detail to diagnose without reproducing it
  by hand -- otherwise a future refactor could delete the ``log.error`` call
  and every exit-code-only test would keep passing.
"""

import json
import runpy
import sys
from pathlib import Path

import httpx
import pytest
import respx

from dawatch.auth import TOKEN_URL
from dawatch.cli import main
from dawatch.client import API_BASE

FEED_URL = f"{API_BASE}/browse/dailydeviations"

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
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DEVIANTART_CLIENT_ID", "cid")
    monkeypatch.setenv("DEVIANTART_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("DAWATCH_NTFY_TOPIC", "my-topic")
    monkeypatch.setenv("DAWATCH_NTFY_URL", "https://ntfy.test")
    monkeypatch.setenv("DAWATCH_DB_PATH", str(tmp_path / "dawatch.db"))
    monkeypatch.setenv("DAWATCH_ENV", "prod")
    monkeypatch.delenv("DAWATCH_PUSHGATEWAY_URL", raising=False)
    return tmp_path


@respx.mock
def test_python_dash_m_dawatch_runs(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m dawatch`` is a documented, packaged entry point.

    Every other test calls ``main()`` in-process. This is the only one that
    runs the ``if __name__ == "__main__"`` guard in ``__main__.py`` itself.
    """
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))

    monkeypatch.setattr(sys, "argv", ["dawatch", "run", "--dry-run"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("dawatch.__main__", run_name="__main__")

    assert exc_info.value.code == 0


@respx.mock
def test_a_fetch_failure_is_logged_with_diagnostic_detail(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(503))

    # One attempt is enough to exhaust retries here; keeps the test fast
    # without changing the behaviour under test.
    monkeypatch.setenv("DAWATCH_MAX_RETRIES", "1")

    exit_code = main(["run"])

    assert exit_code == 1

    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("{")
    ]
    failures = [event for event in events if event.get("event") == "run.failed"]
    assert len(failures) == 1
    assert "error" in failures[0]
    assert failures[0]["error_type"] == "FetchError"

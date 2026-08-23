import pytest
import structlog

from dawatch.config import Settings
from dawatch.logging import configure_logging, redact_secrets


def test_redacts_sensitive_keys() -> None:
    event = {"event": "auth", "access_token": "abc", "client_secret": "xyz"}

    result = redact_secrets(None, "info", event)

    assert result["access_token"] == "***REDACTED***"
    assert result["client_secret"] == "***REDACTED***"


def test_redaction_is_case_insensitive_and_matches_substrings() -> None:
    event = {"event": "req", "Authorization": "Bearer abc", "DA_CLIENT_SECRET": "xyz"}

    result = redact_secrets(None, "info", event)

    assert result["Authorization"] == "***REDACTED***"
    assert result["DA_CLIENT_SECRET"] == "***REDACTED***"


def test_leaves_innocuous_keys_untouched() -> None:
    event = {"event": "fetched", "count": 7, "deviationid": "ABC-123"}

    result = redact_secrets(None, "info", event)

    assert result["count"] == 7
    assert result["deviationid"] == "ABC-123"


def test_configure_logging_emits_json_in_prod(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEVIANTART_CLIENT_ID", "abc")
    monkeypatch.setenv("DEVIANTART_CLIENT_SECRET", "xyz")
    monkeypatch.setenv("DAWATCH_NTFY_TOPIC", "t")
    monkeypatch.setenv("DAWATCH_ENV", "prod")

    configure_logging(Settings.load())
    structlog.get_logger().info("hello", access_token="leak-me")

    captured = capsys.readouterr().out
    assert '"event": "hello"' in captured or '"event":"hello"' in captured
    assert "leak-me" not in captured

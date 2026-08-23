import pytest

from dawatch.config import Settings
from dawatch.errors import ConfigError

REQUIRED_ENV = {
    "DEVIANTART_CLIENT_ID": "abc123",
    "DEVIANTART_CLIENT_SECRET": "s3cret",
    "DAWATCH_NTFY_TOPIC": "my-topic",
}


def test_load_reads_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings.load()

    assert settings.client_id.get_secret_value() == "abc123"
    assert settings.client_secret.get_secret_value() == "s3cret"
    assert settings.ntfy_topic == "my-topic"


def test_load_applies_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings.load()

    assert settings.ntfy_url == "https://ntfy.sh"
    assert settings.env == "prod"
    assert settings.pushgateway_url is None
    assert settings.http_timeout == 10.0
    assert settings.max_retries == 3
    assert settings.notify_mature is False


def test_load_raises_config_error_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEVIANTART_CLIENT_ID", raising=False)
    monkeypatch.delenv("DEVIANTART_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("DAWATCH_NTFY_TOPIC", "my-topic")

    with pytest.raises(ConfigError) as exc_info:
        Settings.load()

    assert "DEVIANTART_CLIENT_ID" in str(exc_info.value)


def test_secrets_are_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    rendered = repr(Settings.load())

    assert "s3cret" not in rendered
    assert "abc123" not in rendered

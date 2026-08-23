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

    message = str(exc_info.value)
    assert "DEVIANTART_CLIENT_ID" in message
    assert "DAWATCH_DEVIANTART" not in message


def test_secrets_are_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    rendered = repr(Settings.load())

    assert "s3cret" not in rendered
    assert "abc123" not in rendered


class TestEnvNameFor:
    """Direct unit tests of the loc-to-env-var-name mapping.

    ``test_load_raises_config_error_when_credentials_missing`` only ever
    exercises the aliased-field branch. These tests pin down all three
    branches individually so a future edit cannot silently regress one
    without a failing test.
    """

    def test_aliased_field_returned_verbatim(self) -> None:
        # DEVIANTART_CLIENT_ID is the alias pydantic reports in `loc` for
        # `client_id`; it is not a key in `model_fields`, so it comes back
        # unchanged rather than being coerced into a DAWATCH_-prefixed name.
        assert Settings._env_name_for(("DEVIANTART_CLIENT_ID",)) == "DEVIANTART_CLIENT_ID"

    def test_non_aliased_field_gets_dawatch_prefix(self) -> None:
        # ntfy_topic has no validation_alias, so `loc` carries the field
        # name itself, and the DAWATCH_ prefix is applied.
        assert Settings._env_name_for(("ntfy_topic",)) == "DAWATCH_NTFY_TOPIC"

    def test_empty_loc_returns_unknown_placeholder(self) -> None:
        assert Settings._env_name_for(()) == "<unknown>"

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from dawatch.auth import TOKEN_URL
from dawatch.errors import AuthError, ConfigError
from dawatch.login import (
    REDIRECT_URI,
    SCOPE,
    build_authorize_url,
    exchange_code,
    validate_callback,
)


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


@respx.mock
def test_rejection_never_echoes_the_client_secret() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, text="secret-in-body"))

    with httpx.Client() as http, pytest.raises(AuthError) as exc_info:
        exchange_code(http, "id", "the-client-secret", "the-code")

    assert "the-client-secret" not in str(exc_info.value)


@respx.mock
def test_transport_failure_raises_auth_error() -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    with httpx.Client() as http, pytest.raises(AuthError):
        exchange_code(http, "id", "secret", "the-code")


@respx.mock
def test_malformed_response_raises_auth_error() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, text="not json at all"))

    with httpx.Client() as http, pytest.raises(AuthError):
        exchange_code(http, "id", "secret", "the-code")


def test_validate_callback_returns_the_code_when_state_matches() -> None:
    assert validate_callback(code="the-code", state="s", error=None, expected_state="s") == (
        "the-code"
    )


def test_validate_callback_rejects_a_mismatched_state() -> None:
    """The state check is the only guard against a forged callback."""
    with pytest.raises(ConfigError, match="state"):
        validate_callback(code="the-code", state="wrong", error=None, expected_state="s")


def test_validate_callback_rejects_a_missing_state() -> None:
    with pytest.raises(ConfigError, match="state"):
        validate_callback(code="the-code", state=None, error=None, expected_state="s")


def test_validate_callback_surfaces_a_refusal() -> None:
    with pytest.raises(ConfigError, match="access_denied"):
        validate_callback(code=None, state="s", error="access_denied", expected_state="s")


def test_validate_callback_reports_a_timeout() -> None:
    with pytest.raises(ConfigError, match="No authorization code"):
        validate_callback(code=None, state="s", error=None, expected_state="s")

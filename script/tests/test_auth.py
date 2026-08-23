from datetime import UTC, datetime, timedelta
from typing import Any

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
def cache() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def auth(cache: InMemoryStore) -> Any:
    with httpx.Client() as http:
        yield DeviantArtAuth(http, "cid", "csecret", cache)


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_requests_a_token_when_cache_is_empty(auth: DeviantArtAuth, cache: InMemoryStore) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    assert auth.token() == "fresh-token"
    assert route.called


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_sends_client_credentials_grant(auth: DeviantArtAuth) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    auth.token()

    body = route.calls.last.request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body
    assert "client_secret=csecret" in body


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_persists_the_token_to_the_cache(auth: DeviantArtAuth, cache: InMemoryStore) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    auth.token()

    stored = cache.load_token()
    assert stored is not None
    assert stored.access_token == "fresh-token"
    assert stored.expires_at == NOW + timedelta(seconds=3600)


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_reuses_a_cached_token_without_calling_the_network(cache: InMemoryStore) -> None:
    cache.save_token(Token(access_token="cached", expires_at=NOW + timedelta(seconds=3600)))
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    with httpx.Client() as http:
        assert DeviantArtAuth(http, "cid", "csecret", cache).token() == "cached"

    assert not route.called


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_replaces_an_expired_cached_token(cache: InMemoryStore) -> None:
    cache.save_token(Token(access_token="stale", expires_at=NOW - timedelta(seconds=1)))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    with httpx.Client() as http:
        assert DeviantArtAuth(http, "cid", "csecret", cache).token() == "fresh-token"


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_caches_within_a_single_run(auth: DeviantArtAuth) -> None:
    """Two calls in one process must not hit the network twice."""
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    auth.token()
    auth.token()

    assert route.call_count == 1


@respx.mock
@time_machine.travel(NOW, tick=False)
def test_invalidate_forces_a_new_token(auth: DeviantArtAuth) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    auth.token()
    auth.invalidate()
    auth.token()

    assert route.call_count == 2


@respx.mock
def test_rejected_credentials_raise_auth_error(auth: DeviantArtAuth) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))

    with pytest.raises(AuthError) as exc_info:
        auth.token()

    assert "csecret" not in str(exc_info.value)


@respx.mock
def test_malformed_token_response_raises_auth_error(auth: DeviantArtAuth) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"status": "success"}))

    with pytest.raises(AuthError):
        auth.token()


@respx.mock
def test_transport_failure_raises_auth_error(auth: DeviantArtAuth) -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(AuthError):
        auth.token()

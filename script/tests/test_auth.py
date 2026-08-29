from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
import time_machine

from dawatch.auth import TOKEN_URL, DeviantArtAuth, RefreshTokenAuth
from dawatch.errors import AuthError, ConfigError
from dawatch.models import Token
from dawatch.store import InMemoryStore, TokenCache

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)

TOKEN_RESPONSE = {
    "status": "success",
    "access_token": "fresh-token",
    "token_type": "Bearer",
    "expires_in": 3600,
}


class SpyCache(TokenCache):
    """Cache that tracks calls to save_token for test assertions."""

    def __init__(self) -> None:
        self._save_calls: int = 0
        self._token: Token | None = None
        self._refresh_token: str | None = None

    def load_token(self) -> Token | None:
        return self._token

    def save_token(self, token: Token) -> None:
        self._save_calls += 1
        self._token = token

    def save_count(self) -> int:
        return self._save_calls

    def load_refresh_token(self) -> str | None:
        return self._refresh_token

    def save_refresh_token(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token


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
@time_machine.travel(NOW, tick=False)
def test_invalidate_does_not_write_to_cache() -> None:
    """invalidate() must perform no store I/O so it is safe on retry paths."""
    spy_cache = SpyCache()
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    with httpx.Client() as http:
        auth = DeviantArtAuth(http, "cid", "csecret", spy_cache)
        auth.token()
        save_count_before = spy_cache.save_count()
        auth.invalidate()

    assert spy_cache.save_count() == save_count_before


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


REFRESH_RESPONSE = {
    "status": "success",
    "access_token": "fresh-token",
    "token_type": "Bearer",
    "expires_in": 3600,
    "refresh_token": "rotated-refresh",
}


def build_refresh_auth(cache: InMemoryStore, seed: str | None = "seed-refresh") -> Any:
    return RefreshTokenAuth(
        httpx.Client(),
        client_id="id",
        client_secret="secret",
        cache=cache,
        seed_refresh_token=seed,
    )


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_sends_the_refresh_token_grant(cache: InMemoryStore) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=REFRESH_RESPONSE))

    assert build_refresh_auth(cache).token() == "fresh-token"

    body = route.calls[0].request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=seed-refresh" in body


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_prefers_a_stored_refresh_token_over_the_seed(cache: InMemoryStore) -> None:
    """The store holds the rotated token; the seed is only a bootstrap."""
    cache.save_refresh_token("stored-refresh")
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=REFRESH_RESPONSE))

    build_refresh_auth(cache).token()

    assert "refresh_token=stored-refresh" in route.calls[0].request.content.decode()


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_persists_a_rotated_refresh_token(cache: InMemoryStore) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=REFRESH_RESPONSE))

    build_refresh_auth(cache).token()

    assert cache.load_refresh_token() == "rotated-refresh"


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_keeps_the_current_token_when_none_is_returned(cache: InMemoryStore) -> None:
    """DeviantArt does not document whether refresh tokens rotate.

    A response without a refresh_token must leave the existing one intact
    rather than clearing it.
    """
    cache.save_refresh_token("stored-refresh")
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

    build_refresh_auth(cache).token()

    assert cache.load_refresh_token() == "stored-refresh"


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_expired_refresh_token_raises_config_error(cache: InMemoryStore) -> None:
    """invalid_grant can never succeed on retry, so it is a config failure.

    Exit 1 would let backoffLimit retry a token that is dead for three months.
    """
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    with pytest.raises(ConfigError, match="dawatch login"):
        build_refresh_auth(cache).token()


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_other_rejections_raise_auth_error(cache: InMemoryStore) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="upstream down"))

    with pytest.raises(AuthError):
        build_refresh_auth(cache).token()


@time_machine.travel(NOW, tick=False)
def test_missing_refresh_token_raises_config_error(cache: InMemoryStore) -> None:
    with pytest.raises(ConfigError, match="dawatch login"):
        build_refresh_auth(cache, seed=None).token()


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_refresh_auth_reuses_a_cached_access_token(cache: InMemoryStore) -> None:
    """Caching behaviour is inherited from the base and must not regress."""
    cache.save_token(Token(access_token="cached", expires_at=NOW + timedelta(hours=1)))
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=REFRESH_RESPONSE))

    assert build_refresh_auth(cache).token() == "cached"
    assert route.call_count == 0


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_refresh_auth_transport_failure_raises_auth_error(cache: InMemoryStore) -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(AuthError):
        build_refresh_auth(cache).token()


@time_machine.travel(NOW, tick=False)
@respx.mock
def test_refresh_auth_malformed_response_raises_auth_error(cache: InMemoryStore) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"no": "token"}))

    with pytest.raises(AuthError):
        build_refresh_auth(cache).token()

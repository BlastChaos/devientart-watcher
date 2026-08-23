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
        # Also clear the cached token since it's been rejected
        self._cache.save_token(Token(access_token="", expires_at=datetime.now(UTC)))

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

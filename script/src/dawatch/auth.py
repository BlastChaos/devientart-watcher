"""OAuth2 authentication, in two grants.

``client_credentials`` authenticates the application: it issues no refresh
token, so expiry is handled by repeating the token request. ``refresh_token``
authenticates the user, which is the only way to see who they watch.

Both cache the access token across process runs, because a scheduled one-shot
job would otherwise authenticate on every single invocation.
"""

from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import structlog

from dawatch.errors import AuthError, ConfigError
from dawatch.models import Token
from dawatch.store import TokenCache

TOKEN_URL = "https://www.deviantart.com/oauth2/token"

log = structlog.get_logger(__name__)


class TokenProvider(Protocol):
    def token(self) -> str: ...

    def invalidate(self) -> None: ...


class _CachedTokenAuth:
    """Access-token caching shared by every grant.

    Subclasses supply only ``_request_token``. The caching rules — in-process
    memory, then the persistent cache, then the network — are identical
    regardless of how the token is obtained.
    """

    def __init__(self, cache: TokenCache) -> None:
        self._cache = cache
        self._current: Token | None = None
        self._force_refresh: bool = False

    def token(self) -> str:
        """Return a usable access token, fetching a new one only if needed."""
        now = datetime.now(UTC)

        if not self._force_refresh:
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
        self._force_refresh = False
        log.info("token.refreshed", expires_at=fresh.expires_at.isoformat())
        return fresh.access_token

    def invalidate(self) -> None:
        """Force the next call to re-authenticate, skipping all caches.

        Called when the API rejects a token we believed was live, which can
        happen if DeviantArt revokes it early. This sets a flag that forces
        the next token() call to bypass both in-process and persistent caches
        and fetch a fresh token directly from the token endpoint.
        """
        self._current = None
        self._force_refresh = True

    def _request_token(self, now: datetime) -> Token:
        raise NotImplementedError


class DeviantArtAuth(_CachedTokenAuth):
    """Supplies a bearer token for the application itself.

    This grant has no user, so it cannot read any user-scoped feed.
    """

    def __init__(
        self,
        http: httpx.Client,
        client_id: str,
        client_secret: str,
        cache: TokenCache,
    ) -> None:
        super().__init__(cache)
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret

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


class RefreshTokenAuth(_CachedTokenAuth):
    """Supplies a bearer token that acts as the user, not the application.

    The refresh token is read from the store first and the configured seed
    second, so a token rotated by an earlier run always wins over the value
    baked into the environment.
    """

    def __init__(
        self,
        http: httpx.Client,
        client_id: str,
        client_secret: str,
        cache: TokenCache,
        seed_refresh_token: str | None = None,
    ) -> None:
        super().__init__(cache)
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._seed = seed_refresh_token

    def _request_token(self, now: datetime) -> Token:
        refresh_token = self._cache.load_refresh_token() or self._seed
        if not refresh_token:
            raise ConfigError(
                "No refresh token available. Run 'dawatch login' and store the "
                "result as DAWATCH_REFRESH_TOKEN."
            )

        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                },
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"Could not reach the token endpoint: {type(exc).__name__}") from exc

        if response.status_code != httpx.codes.OK:
            self._raise_for_rejection(response)

        try:
            payload: dict[str, Any] = response.json()
            token = Token.from_response(payload, now)
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthError("Token endpoint returned a malformed response") from exc

        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != refresh_token:
            self._cache.save_refresh_token(rotated)
            log.info("token.refresh_token_rotated")

        return token

    @staticmethod
    def _raise_for_rejection(response: httpx.Response) -> None:
        """Separate a dead refresh token from every other rejection.

        A refresh token expires after three months and cannot be revived by
        retrying, so it has to surface as a configuration failure and stop the
        CronJob's backoffLimit. Only the 'error' field is read: the rest of
        the body can echo credentials.
        """
        error_code = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error_code = str(body.get("error", ""))
        except ValueError:
            pass

        if error_code == "invalid_grant":
            raise ConfigError(
                "The refresh token has expired or been revoked. Run 'dawatch login' "
                "and store the new DAWATCH_REFRESH_TOKEN."
            )

        raise AuthError(
            f"Token endpoint rejected the refresh grant (HTTP {response.status_code}). "
            f"Check DEVIANTART_CLIENT_ID and DEVIANTART_CLIENT_SECRET."
        )

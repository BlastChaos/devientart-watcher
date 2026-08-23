"""HTTP access to the DeviantArt API.

Retries cover connection errors, 429, and 5xx. They deliberately do not cover
other 4xx responses: those indicate a defect in our request, and retrying
would burn quota without any chance of succeeding.
"""

import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from dawatch.auth import TokenProvider
from dawatch.errors import FetchError

API_BASE = "https://www.deviantart.com/api/v1/oauth2"
API_VERSION = "20240701"

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Clamp Retry-After to prevent sleep bursts that would hold a CronJob slot.
# The Kubernetes pod's startingDeadlineSeconds is 300 (5 minutes).
MAX_RETRY_AFTER_SECONDS = 60

log = structlog.get_logger(__name__)


class DeviantArtClient:
    """Authenticated JSON GETs against the DeviantArt API."""

    def __init__(
        self,
        http: httpx.Client,
        auth: TokenProvider,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._auth = auth
        self._max_retries = max(1, max_retries)
        self._sleep = sleep

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """GET ``path`` and return the decoded JSON body.

        Raises:
            FetchError: on a non-retryable status, exhausted retries, or a
                body that is not JSON.
        """
        url = f"{API_BASE}/{path.lstrip('/')}"
        reauthed = False
        last_problem = "unknown"
        attempt = 0

        while attempt < self._max_retries:
            try:
                response = self._http.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._auth.token()}",
                        "dA-minor-version": API_VERSION,
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                last_problem = type(exc).__name__
                log.warning("fetch.transport_error", attempt=attempt + 1, error=last_problem)
                self._wait(attempt, None)
                attempt += 1
                continue

            status = response.status_code

            if status == httpx.codes.OK:
                return self._decode(response)

            if status == httpx.codes.UNAUTHORIZED and not reauthed:
                # The token was accepted at issue time but is no longer
                # valid. Re-authenticate once; a loop here would hammer the
                # token endpoint on genuinely bad credentials.
                log.info("fetch.reauthenticating")
                reauthed = True
                self._auth.invalidate()
                continue

            if status not in RETRYABLE_STATUS:
                raise FetchError(f"GET {path} failed with HTTP {status}")

            last_problem = f"HTTP {status}"
            log.warning("fetch.retryable_status", attempt=attempt + 1, status=status)
            self._wait(attempt, response.headers.get("Retry-After"))
            attempt += 1

        raise FetchError(f"GET {path} failed after {self._max_retries} attempts: {last_problem}")

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        """Sleep before the next attempt, preferring the server's own advice."""
        if retry_after is not None:
            try:
                delay = max(0.0, min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
                self._sleep(delay)
                return
            except ValueError:
                # Retry-After may be an HTTP-date; fall back to backoff.
                pass
        self._sleep(float(2**attempt))

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise FetchError("API returned a body that is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise FetchError(f"API returned {type(payload).__name__}, expected an object")
        return payload

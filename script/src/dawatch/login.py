"""One-time interactive consent for the authorization_code grant.

A CronJob cannot open a browser, so the refresh token is minted here, on a
workstation, and handed to the operator to place in OpenBao. This module runs
roughly once every three months and never inside the cluster.
"""

import base64
import hashlib
import http.server
import secrets
import threading
import urllib.parse
import webbrowser
from typing import Any

import httpx
import structlog

from dawatch.auth import TOKEN_URL
from dawatch.errors import AuthError, ConfigError

AUTHORIZE_URL = "https://www.deviantart.com/oauth2/authorize"
REDIRECT_URI = "http://localhost:8080/callback"
# The watch feed lives behind 'browse'. If DeviantArt disagrees, consent fails
# visibly here rather than as an empty feed in a scheduled run months later.
SCOPE = "browse"
CALLBACK_TIMEOUT_SECONDS = 300

log = structlog.get_logger(__name__)


def generate_pkce_pair() -> tuple[str, str]:
    """Return a (code_verifier, code_challenge) pair for one consent attempt.

    token_urlsafe(64) yields 86 characters, inside the 43-128 the spec allows.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorize_url(
    client_id: str,
    state: str,
    code_challenge: str,
    redirect_uri: str = REDIRECT_URI,
    scope: str = SCOPE,
) -> str:
    """Return the URL the user must visit to grant consent.

    DeviantArt is an OAuth 2.1 provider, so PKCE is mandatory and S256 is the
    only accepted method. Omitting the challenge makes /oauth2/authorize
    answer invalid_request.
    """
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(
    http: httpx.Client,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str = REDIRECT_URI,
) -> str:
    """Trade an authorization code for a refresh token.

    Raises:
        AuthError: if the token endpoint refuses the code or answers with
            something that is not a token.
        ConfigError: if the exchange succeeds but carries no refresh token,
            which means the granted scope was not the one requested.
    """
    try:
        response = http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach the token endpoint: {type(exc).__name__}") from exc

    if response.status_code != httpx.codes.OK:
        # The body is deliberately not included: it can echo credentials.
        raise AuthError(
            f"Token endpoint rejected the authorization code (HTTP {response.status_code})."
        )

    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise AuthError("Token endpoint returned a malformed response") from exc

    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConfigError(
            "The authorization succeeded but returned no refresh token. "
            f"Confirm the application is registered with the '{SCOPE}' scope."
        )

    return refresh_token


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the single redirect DeviantArt sends back."""

    code: str | None = None
    state: str | None = None
    error: str | None = None

    # The method name is fixed by BaseHTTPRequestHandler's dispatch.
    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = query.get("code", [None])[0]
        _CallbackHandler.state = query.get("state", [None])[0]
        _CallbackHandler.error = query.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"dawatch: authorization received. You can close this tab.")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default stderr access log.

        The 'format' parameter shadows a builtin, but the name is part of the
        signature BaseHTTPRequestHandler calls.
        """


def run_consent_flow(http_client: httpx.Client, client_id: str, client_secret: str) -> str:
    """Open a browser, capture the redirect, and return the refresh token.

    Raises:
        ConfigError: if consent is refused, times out, or comes back with a
            state that does not match the one sent.
        AuthError: if the code cannot be exchanged.
    """
    expected_state = secrets.token_urlsafe(16)
    code_verifier, code_challenge = generate_pkce_pair()
    _CallbackHandler.code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None

    server = http.server.HTTPServer(("localhost", 8080), _CallbackHandler)
    server.timeout = CALLBACK_TIMEOUT_SECONDS

    url = build_authorize_url(client_id, expected_state, code_challenge)
    print(f"Opening your browser to authorize dawatch.\nIf it does not open: {url}\n")
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        server.handle_request()
    finally:
        server.server_close()

    code = validate_callback(
        code=_CallbackHandler.code,
        state=_CallbackHandler.state,
        error=_CallbackHandler.error,
        expected_state=expected_state,
    )
    return exchange_code(http_client, client_id, client_secret, code, code_verifier)


def validate_callback(
    code: str | None,
    state: str | None,
    error: str | None,
    expected_state: str,
) -> str:
    """Return the authorization code, or explain why there is not one.

    Separated from the socket handling so every refusal path is testable
    without binding a port -- including the state check, which is the only
    thing standing between this flow and a forged callback.

    Raises:
        ConfigError: if consent was refused, nothing arrived, or the state
            does not match the one this process sent.
    """
    if error:
        raise ConfigError(f"Authorization was refused: {error}")
    if code is None:
        raise ConfigError(
            f"No authorization code arrived within {CALLBACK_TIMEOUT_SECONDS} seconds."
        )
    if not secrets.compare_digest(state or "", expected_state):
        # A mismatched state means the response did not originate from the
        # request this process started.
        raise ConfigError("Authorization state did not match the request. Start over.")
    return code

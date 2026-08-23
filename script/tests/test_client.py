from collections.abc import Generator

import httpx
import pytest
import respx

from dawatch.client import API_BASE, API_VERSION, DeviantArtClient
from dawatch.errors import FetchError

FEED_URL = f"{API_BASE}/browse/dailydeviations"


class StubAuth:
    def __init__(self) -> None:
        self.tokens = ["token-1", "token-2"]
        self.invalidated = 0

    def token(self) -> str:
        return self.tokens[min(self.invalidated, len(self.tokens) - 1)]

    def invalidate(self) -> None:
        self.invalidated += 1


@pytest.fixture
def auth() -> StubAuth:
    return StubAuth()


@pytest.fixture
def client(auth: StubAuth) -> Generator[DeviantArtClient, None, None]:
    """No real sleeping: retry tests must not take seconds of wall clock."""
    with httpx.Client() as http:
        yield DeviantArtClient(http, auth, max_retries=3, sleep=lambda _: None)


@respx.mock
def test_returns_decoded_json(client: DeviantArtClient) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    assert client.get_json("browse/dailydeviations") == {"results": []}


@respx.mock
def test_sends_bearer_token_and_version_header(client: DeviantArtClient) -> None:
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={}))

    client.get_json("browse/dailydeviations")

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer token-1"
    assert request.headers["dA-minor-version"] == API_VERSION


@respx.mock
def test_forwards_query_parameters(client: DeviantArtClient) -> None:
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={}))

    client.get_json("browse/dailydeviations", {"date": "2026-08-23"})

    assert route.calls.last.request.url.params["date"] == "2026-08-23"


@respx.mock
def test_retries_on_server_error_then_succeeds(client: DeviantArtClient) -> None:
    route = respx.get(FEED_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"results": [{"deviationid": "A"}]}),
        ]
    )

    result = client.get_json("browse/dailydeviations")

    assert route.call_count == 2
    assert result["results"][0]["deviationid"] == "A"


@respx.mock
def test_retries_on_connect_error(client: DeviantArtClient) -> None:
    route = respx.get(FEED_URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})]
    )

    client.get_json("browse/dailydeviations")

    assert route.call_count == 2


@respx.mock
def test_honours_retry_after_header(auth: StubAuth) -> None:
    delays: list[float] = []
    respx.get(FEED_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={}),
        ]
    )

    with httpx.Client() as http:
        client = DeviantArtClient(http, auth, max_retries=3, sleep=delays.append)
        client.get_json("browse/dailydeviations")

    assert delays == [7.0]


@respx.mock
def test_backs_off_exponentially_without_retry_after(auth: StubAuth) -> None:
    delays: list[float] = []
    respx.get(FEED_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200, json={})]
    )

    with httpx.Client() as http:
        client = DeviantArtClient(http, auth, max_retries=3, sleep=delays.append)
        client.get_json("browse/dailydeviations")

    assert delays == [1.0, 2.0]


@respx.mock
def test_gives_up_after_max_retries(client: DeviantArtClient) -> None:
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(FetchError) as exc_info:
        client.get_json("browse/dailydeviations")

    assert route.call_count == 3
    assert "503" in str(exc_info.value)


@respx.mock
def test_retries_once_on_401_after_invalidating_the_token(
    client: DeviantArtClient, auth: StubAuth
) -> None:
    """A revoked token deserves exactly one re-auth, not a retry storm."""
    route = respx.get(FEED_URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={})]
    )

    client.get_json("browse/dailydeviations")

    assert auth.invalidated == 1
    assert route.call_count == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer token-2"


@respx.mock
def test_does_not_retry_a_client_error(client: DeviantArtClient) -> None:
    """A 400 is a defect in our request; retrying just wastes quota."""
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(400))

    with pytest.raises(FetchError):
        client.get_json("browse/dailydeviations")

    assert route.call_count == 1


@respx.mock
def test_malformed_json_raises_fetch_error(client: DeviantArtClient) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=b"<html>nope</html>"))

    with pytest.raises(FetchError):
        client.get_json("browse/dailydeviations")

from collections.abc import Generator

import httpx
import pytest
import respx

from dawatch.errors import NotifyError
from dawatch.models import Deviation
from dawatch.notifier import ConsoleNotifier, NtfyNotifier, RecordingNotifier

TOPIC_URL = "https://ntfy.sh/my-topic"

DEVIATION = Deviation.model_validate(
    {
        "deviationid": "ABC-123",
        "title": "Neon Alley",
        "url": "https://www.deviantart.com/artist/art/neon-alley",
        "author": {"username": "artist"},
        "preview": {"src": "https://images.invalid/preview.jpg"},
    }
)


@pytest.fixture
def notifier() -> Generator[NtfyNotifier]:
    with httpx.Client() as http:
        yield NtfyNotifier(http, "https://ntfy.sh", "my-topic")


@respx.mock
def test_posts_to_the_topic_url(notifier: NtfyNotifier) -> None:
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))

    notifier.send(DEVIATION)

    assert route.called


@respx.mock
def test_sends_title_and_body(notifier: NtfyNotifier) -> None:
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    notifier.send(DEVIATION)

    headers = route.calls.last.request.headers
    assert headers["X-Title"] == "Neon Alley"
    assert "artist" in route.calls.last.request.content.decode()


@respx.mock
def test_sets_click_and_attach_headers(notifier: NtfyNotifier) -> None:
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    notifier.send(DEVIATION)

    headers = route.calls.last.request.headers
    assert headers["X-Click"] == "https://www.deviantart.com/artist/art/neon-alley"
    assert headers["X-Attach"] == "https://images.invalid/preview.jpg"


@respx.mock
def test_omits_optional_headers_when_data_is_absent(notifier: NtfyNotifier) -> None:
    """ntfy rejects empty header values, so absent data means absent header."""
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    notifier.send(Deviation.model_validate({"deviationid": "X"}))

    headers = route.calls.last.request.headers
    assert "X-Click" not in headers
    assert "X-Attach" not in headers


@respx.mock
def test_encodes_non_ascii_titles(notifier: NtfyNotifier) -> None:
    """ntfy headers are latin-1; a unicode title must not raise."""
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    notifier.send(Deviation.model_validate({"deviationid": "X", "title": "Sünset 日"}))

    assert route.called
    assert route.calls.last.request.headers["X-Title"].isascii()


@respx.mock
def test_strips_trailing_slash_from_base_url() -> None:
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={}))

    with httpx.Client() as http:
        NtfyNotifier(http, "https://ntfy.sh/", "my-topic").send(DEVIATION)

    assert route.called


@respx.mock
def test_server_error_raises_notify_error(notifier: NtfyNotifier) -> None:
    respx.post(TOPIC_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(NotifyError) as exc_info:
        notifier.send(DEVIATION)

    assert "500" in str(exc_info.value)


@respx.mock
def test_transport_failure_raises_notify_error(notifier: NtfyNotifier) -> None:
    respx.post(TOPIC_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(NotifyError):
        notifier.send(DEVIATION)


def test_console_notifier_prints(capsys: pytest.CaptureFixture[str]) -> None:
    ConsoleNotifier().send(DEVIATION)

    out = capsys.readouterr().out
    assert "Neon Alley" in out
    assert "artist" in out


def test_recording_notifier_collects_sends() -> None:
    notifier = RecordingNotifier()

    notifier.send(DEVIATION)

    assert notifier.sent == [DEVIATION]


def test_recording_notifier_can_be_told_to_fail() -> None:
    notifier = RecordingNotifier(fail_ids={"ABC-123"})

    with pytest.raises(NotifyError):
        notifier.send(DEVIATION)

    assert notifier.sent == []


NON_ASCII_URL_DEVIATION = Deviation.model_validate(
    {
        "deviationid": "NON-ASCII",
        "title": "Cafe",
        "url": "https://www.deviantart.com/user/art/Café-123",
        "author": {"username": "artist"},
        "preview": {"src": "https://images.invalid/ü.jpg"},
    }
)


@respx.mock
def test_non_ascii_url_does_not_break_header_encoding(notifier: NtfyNotifier) -> None:
    """A URL with non-ASCII characters must not raise LocalProtocolError.

    httpx encodes headers as latin-1. An unescaped non-ASCII URL makes it
    refuse to build the request at all, which left the deviation unseen and
    retried on every subsequent run.
    """
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))

    notifier.send(NON_ASCII_URL_DEVIATION)

    sent = route.calls[0].request
    assert sent.headers["X-Click"] == "https://www.deviantart.com/user/art/Caf%C3%A9-123"


@respx.mock
def test_non_ascii_image_url_is_encoded(notifier: NtfyNotifier) -> None:
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))

    notifier.send(NON_ASCII_URL_DEVIATION)

    sent = route.calls[0].request
    assert sent.headers["X-Attach"] == "https://images.invalid/%C3%BC.jpg"


@respx.mock
def test_already_encoded_url_is_not_double_encoded(notifier: NtfyNotifier) -> None:
    """A percent sign in an already-encoded URL must survive untouched."""
    deviation = Deviation.model_validate(
        {
            "deviationid": "ENCODED",
            "title": "Encoded",
            "url": "https://www.deviantart.com/user/art/Caf%C3%A9-123",
            "author": {"username": "artist"},
        }
    )
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))

    notifier.send(deviation)

    sent = route.calls[0].request
    assert sent.headers["X-Click"] == "https://www.deviantart.com/user/art/Caf%C3%A9-123"

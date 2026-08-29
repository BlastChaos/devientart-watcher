"""Notification delivery.

ntfy carries the message body as the request body and its metadata as
headers. HTTP headers are latin-1, so any non-ASCII text is escaped before
it goes into a header value, and HTTP forbids a value that begins or ends
with whitespace or contains a line break, so whitespace is collapsed first.
"""

import re
from typing import Protocol
from urllib.parse import quote

import httpx
import structlog

from dawatch.errors import NotifyError
from dawatch.models import Deviation

log = structlog.get_logger(__name__)


_WHITESPACE_RUN = re.compile(r"\s+")


def _header_safe(value: str) -> str:
    """Make a string safe for an HTTP header value.

    Every run of whitespace becomes a single space and the result is
    stripped: a header value that starts or ends with whitespace, or that
    carries a line break, is rejected before it reaches the wire, and
    DeviantArt titles do arrive with trailing newlines.

    Non-ASCII characters are backslash-escaped rather than dropped, so a
    title in Japanese still tells the reader something.
    """
    collapsed = _WHITESPACE_RUN.sub(" ", value).strip()
    return collapsed.encode("ascii", "backslashreplace").decode("ascii")


# Characters legal in a URL, plus '%' so an already-encoded URL is not
# encoded a second time on its way into the header.
_URL_SAFE_CHARS = ":/?#[]@!$&'()*+,;=~-._%"


def _url_safe(value: str) -> str:
    """Percent-encode a URL for use as an HTTP header value.

    Titles are backslash-escaped because a mangled title still reads. A URL
    has to stay clickable, so it is percent-encoded instead. Surrounding
    whitespace is dropped rather than encoded: a leading "%20" would break
    the link, and the whitespace was never part of the URL.
    """
    return quote(value.strip(), safe=_URL_SAFE_CHARS)


class Notifier(Protocol):
    def send(self, deviation: Deviation) -> None: ...


class NtfyNotifier:
    """Publishes one notification per deviation to an ntfy topic."""

    def __init__(self, http: httpx.Client, base_url: str, topic: str) -> None:
        self._http = http
        self._url = f"{base_url.rstrip('/')}/{topic}"

    def send(self, deviation: Deviation) -> None:
        """Publish a single deviation.

        Raises:
            NotifyError: if the topic could not be reached or refused the
                message. The caller leaves the deviation unseen so the next
                run retries it.
        """
        headers = {
            "X-Tags": "art",
            "X-Priority": "default",
        }
        # Each optional header is set only when it has content: ntfy refuses an
        # empty header value, and sanitising can empty one out.
        if title := _header_safe(deviation.title):
            headers["X-Title"] = title
        if deviation.url and (click := _url_safe(deviation.url)):
            headers["X-Click"] = click
        if deviation.image_url and (attach := _url_safe(deviation.image_url)):
            headers["X-Attach"] = attach

        body = f"New Daily Deviation by {deviation.author_name}"

        try:
            response = self._http.post(self._url, content=body.encode(), headers=headers)
        except httpx.HTTPError as exc:
            # The exception text is kept: the class name alone does not say
            # which header or URL the request builder objected to.
            raise NotifyError(
                f"Could not reach ntfy for {deviation.deviationid}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code != httpx.codes.OK:
            raise NotifyError(
                f"ntfy refused {deviation.deviationid} with HTTP {response.status_code}"
            )

        log.info("notify.sent", deviationid=deviation.deviationid, title=deviation.title)


class ConsoleNotifier:
    """Prints instead of sending. Used by --dry-run."""

    def send(self, deviation: Deviation) -> None:
        print(f"[dry-run] {deviation.title} by {deviation.author_name} -> {deviation.url or '-'}")


class RecordingNotifier:
    """Test double that records sends and can be told to fail for given ids."""

    def __init__(self, fail_ids: set[str] | None = None) -> None:
        self.sent: list[Deviation] = []
        self._fail_ids = fail_ids or set()

    def send(self, deviation: Deviation) -> None:
        if deviation.deviationid in self._fail_ids:
            raise NotifyError(f"forced failure for {deviation.deviationid}")
        self.sent.append(deviation)

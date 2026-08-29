from typing import Any

import pytest
import structlog

from dawatch.errors import FetchError
from dawatch.sources import DailyDeviationsSource, WatchedDeviationsSource

PAYLOAD: dict[str, Any] = {
    "has_more": False,
    "results": [
        {"deviationid": "A", "title": "First", "author": {"username": "alice"}},
        {"deviationid": "B", "title": "Second", "author": {"username": "bob"}},
    ],
}


class StubClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append((path, params))
        return self.payload


def test_returns_parsed_deviations() -> None:
    source = DailyDeviationsSource(StubClient(PAYLOAD))

    deviations = source.fetch()

    assert [d.deviationid for d in deviations] == ["A", "B"]
    assert deviations[0].author_name == "alice"


def test_requests_the_daily_deviations_path() -> None:
    client = StubClient(PAYLOAD)

    DailyDeviationsSource(client).fetch()

    assert client.calls[0][0] == "browse/dailydeviations"


def test_sends_no_params_when_no_date_given() -> None:
    """The endpoint defaults to today; sending date= explicitly is noise."""
    client = StubClient(PAYLOAD)

    DailyDeviationsSource(client).fetch()

    assert client.calls[0][1] is None


def test_sends_the_date_when_given() -> None:
    client = StubClient(PAYLOAD)

    DailyDeviationsSource(client).fetch("2026-08-01")

    assert client.calls[0][1] == {"date": "2026-08-01"}


def test_rejects_a_malformed_date_before_making_a_request() -> None:
    client = StubClient(PAYLOAD)

    with pytest.raises(FetchError) as exc_info:
        DailyDeviationsSource(client).fetch("01-08-2026")

    assert client.calls == []
    assert "YYYY-MM-DD" in str(exc_info.value)


def test_handles_an_empty_feed() -> None:
    source = DailyDeviationsSource(StubClient({"results": [], "has_more": False}))

    assert source.fetch() == []


def test_unparseable_payload_raises_fetch_error() -> None:
    source = DailyDeviationsSource(StubClient({"results": "not-a-list"}))

    with pytest.raises(FetchError):
        source.fetch()


def test_skips_a_single_malformed_result_rather_than_failing_the_run() -> None:
    """One bad row must not cost the user every other notification."""
    payload = {"results": [{"deviationid": "A"}, {"title": "no id"}], "has_more": False}

    deviations = DailyDeviationsSource(StubClient(payload)).fetch()

    assert [d.deviationid for d in deviations] == ["A"]


def test_logs_diagnostic_context_when_skipping_malformed_result() -> None:
    """Skipped row warnings include error detail and raw deviationid for diagnostics."""
    payload = {"results": [{"deviationid": "id-123", "author": "not-an-object"}], "has_more": False}

    with structlog.testing.capture_logs() as cap_logs:
        DailyDeviationsSource(StubClient(payload)).fetch()

    warning_logs = [log for log in cap_logs if log["event"] == "source.skipped_malformed_result"]
    assert len(warning_logs) == 1
    assert "error" in warning_logs[0]
    assert "raw_id" in warning_logs[0]
    assert warning_logs[0]["raw_id"] == "id-123"


class PagingStubClient:
    """Returns a scripted sequence of pages and records the params it saw."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, str] | None] = []

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append(params)
        return self.pages[len(self.calls) - 1]


def _page(ids: list[str], has_more: bool, next_offset: int | None = None) -> dict[str, Any]:
    return {
        "has_more": has_more,
        "next_offset": next_offset,
        "results": [
            {"deviationid": i, "title": f"Art {i}", "author": {"username": "alice"}} for i in ids
        ],
    }


def test_requests_the_watch_feed_path() -> None:
    client = PagingStubClient([_page(["A"], has_more=False)])

    WatchedDeviationsSource(client, seen=lambda _: False).fetch()

    assert client.calls[0] == {"offset": "0", "limit": "50"}


def test_stops_at_the_first_already_seen_deviation() -> None:
    """The stop rule is what makes a quiet run cost one request."""
    client = PagingStubClient([_page(["NEW1", "NEW2", "OLD", "OLDER"], has_more=True)])
    source = WatchedDeviationsSource(client, seen=lambda d: d.startswith("OLD"))

    result = source.fetch()

    assert [d.deviationid for d in result] == ["NEW1", "NEW2"]
    assert len(client.calls) == 1


def test_pages_forward_while_everything_is_new() -> None:
    client = PagingStubClient(
        [
            _page(["A", "B"], has_more=True, next_offset=50),
            _page(["C", "SEEN"], has_more=True, next_offset=100),
        ]
    )
    source = WatchedDeviationsSource(client, seen=lambda d: d == "SEEN")

    result = source.fetch()

    assert [d.deviationid for d in result] == ["A", "B", "C"]
    assert client.calls[1] == {"offset": "50", "limit": "50"}


def test_stops_when_the_api_reports_no_more_pages() -> None:
    client = PagingStubClient([_page(["A"], has_more=False)])

    assert len(WatchedDeviationsSource(client, seen=lambda _: False).fetch()) == 1
    assert len(client.calls) == 1


def test_respects_the_page_cap() -> None:
    """A first run against a large watch list must terminate."""
    client = PagingStubClient(
        [_page([f"P{n}"], has_more=True, next_offset=n + 1) for n in range(10)]
    )
    source = WatchedDeviationsSource(client, seen=lambda _: False, max_pages=3)

    result = source.fetch()

    assert len(client.calls) == 3
    assert len(result) == 3


def test_falls_back_to_a_computed_offset_when_next_offset_is_absent() -> None:
    client = PagingStubClient([_page(["A", "B"], has_more=True), _page(["C"], has_more=False)])

    WatchedDeviationsSource(client, seen=lambda _: False).fetch()

    assert client.calls[1] == {"offset": "2", "limit": "50"}


def test_rejects_a_date_argument() -> None:
    """The watch feed has no date parameter, so --date must fail loudly."""
    client = PagingStubClient([_page(["A"], has_more=False)])

    with pytest.raises(FetchError, match="date"):
        WatchedDeviationsSource(client, seen=lambda _: False).fetch("2026-08-01")


def test_watch_feed_skips_a_malformed_result_without_losing_the_page() -> None:
    client = PagingStubClient(
        [
            {
                "has_more": False,
                "results": [
                    {"deviationid": "A", "title": "Good", "author": {"username": "alice"}},
                    {"title": "No id at all"},
                    {"deviationid": "C", "title": "Also good", "author": {"username": "bob"}},
                ],
            }
        ]
    )

    result = WatchedDeviationsSource(client, seen=lambda _: False).fetch()

    assert [d.deviationid for d in result] == ["A", "C"]


def test_watch_feed_unparseable_payload_raises_fetch_error() -> None:
    client = PagingStubClient([{"results": "not a list"}])

    with pytest.raises(FetchError):
        WatchedDeviationsSource(client, seen=lambda _: False).fetch()


def test_page_size_is_sent_on_every_request() -> None:
    client = PagingStubClient(
        [_page(["A"], has_more=True, next_offset=10), _page(["B"], has_more=False)]
    )

    WatchedDeviationsSource(client, seen=lambda _: False, page_size=10).fetch()

    assert client.calls == [{"offset": "0", "limit": "10"}, {"offset": "10", "limit": "10"}]

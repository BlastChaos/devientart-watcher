from typing import Any

import pytest
import structlog

from dawatch.errors import FetchError
from dawatch.sources import DailyDeviationsSource

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

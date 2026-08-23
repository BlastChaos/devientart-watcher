"""Sources of deviations.

Only the Daily Deviations feed is implemented. The 'artists you watch' feed
needs the OAuth2 authorization code flow, so it becomes a second
DeviationSource later without any change to the orchestration layer.
"""

import re
from datetime import datetime
from typing import Any, Protocol

import structlog
from pydantic import ValidationError

from dawatch.errors import FetchError
from dawatch.models import Deviation

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

log = structlog.get_logger(__name__)


class JsonClient(Protocol):
    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]: ...


class DeviationSource(Protocol):
    def fetch(self, date: str | None = None) -> list[Deviation]: ...


class DailyDeviationsSource:
    """The staff-picked Daily Deviations for one day.

    The endpoint takes only a ``date``. It has no limit and no offset, so a
    single request returns the whole day and there is no pagination.
    """

    PATH = "browse/dailydeviations"

    def __init__(self, client: JsonClient) -> None:
        self._client = client

    def fetch(self, date: str | None = None) -> list[Deviation]:
        """Return the day's deviations, newest feed first.

        Raises:
            FetchError: if ``date`` is malformed or the payload cannot be
                interpreted as a feed at all.
        """
        params = {"date": self._validate_date(date)} if date is not None else None

        payload = self._client.get_json(self.PATH, params)
        raw_results = payload.get("results", [])

        if not isinstance(raw_results, list):
            raise FetchError(
                f"Feed payload had {type(raw_results).__name__} results, expected a list"
            )

        deviations: list[Deviation] = []
        for raw in raw_results:
            try:
                deviations.append(Deviation.model_validate(raw))
            except ValidationError:
                # One unusable row should not cost the user every other
                # notification in the batch.
                log.warning("source.skipped_malformed_result")

        log.info("source.fetched", count=len(deviations), date=date or "today")
        return deviations

    @staticmethod
    def _validate_date(date: str) -> str:
        if not DATE_PATTERN.match(date):
            raise FetchError(f"Date {date!r} is not in YYYY-MM-DD format")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise FetchError(f"Date {date!r} is not a real calendar date") from exc
        return date

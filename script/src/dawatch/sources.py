"""Sources of deviations.

Two feeds, one Protocol. Daily Deviations is a single dated request with no
pagination; the watch feed is an offset-paged stream with no date at all. The
orchestration layer sees neither difference.
"""

import re
from collections.abc import Callable
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
        """Return the day's deviations from the API.

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
            except ValidationError as exc:
                # One unusable row should not cost the user every other
                # notification in the batch.
                log.warning(
                    "source.skipped_malformed_result",
                    error=str(exc),
                    raw_id=raw.get("deviationid"),
                )

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


class WatchedDeviationsSource:
    """Deviations posted by the artists the authenticated user watches.

    Requires a user-scoped token: the client_credentials grant authenticates
    the application, which watches nobody.

    Paging stops at the first deviation already in the store, so a quiet run
    costs exactly one request while a run after an outage still catches up.
    MAX_PAGES bounds the first run against a large watch list.
    """

    PATH = "browse/deviantsyouwatch"
    PAGE_SIZE = 50
    MAX_PAGES = 5

    def __init__(
        self,
        client: JsonClient,
        seen: Callable[[str], bool],
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._client = client
        self._seen = seen
        self._page_size = page_size
        self._max_pages = max_pages

    def fetch(self, date: str | None = None) -> list[Deviation]:
        """Return everything posted since the last already-seen deviation.

        Raises:
            FetchError: if ``date`` is supplied, or a page cannot be
                interpreted as a feed at all.
        """
        if date is not None:
            raise FetchError(
                "The watched-artists feed has no date parameter. "
                "Remove --date, or use the Daily Deviations source."
            )

        collected: list[Deviation] = []
        offset = 0
        pages_read = 0

        while pages_read < self._max_pages:
            payload = self._client.get_json(
                self.PATH, {"offset": str(offset), "limit": str(self._page_size)}
            )
            pages_read += 1

            raw_results = payload.get("results", [])
            if not isinstance(raw_results, list):
                raise FetchError(
                    f"Feed payload had {type(raw_results).__name__} results, expected a list"
                )

            reached_seen = False
            for raw in raw_results:
                deviation = self._parse(raw)
                if deviation is None:
                    continue
                if self._seen(deviation.deviationid):
                    reached_seen = True
                    break
                collected.append(deviation)

            if reached_seen or not payload.get("has_more"):
                break

            next_offset = payload.get("next_offset")
            offset = int(next_offset) if next_offset is not None else offset + len(raw_results)

        log.info(
            "source.fetched",
            count=len(collected),
            pages=pages_read,
            feed="deviantsyouwatch",
        )
        return collected

    @staticmethod
    def _parse(raw: Any) -> Deviation | None:
        """Return a Deviation, or None if this single row is unusable."""
        try:
            return Deviation.model_validate(raw)
        except ValidationError as exc:
            # One unusable row should not cost the user every other
            # notification in the batch.
            log.warning(
                "source.skipped_malformed_result",
                error=str(exc),
                raw_id=raw.get("deviationid") if isinstance(raw, dict) else None,
            )
            return None

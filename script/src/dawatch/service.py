"""Orchestration.

This module depends only on the four protocols, never on httpx, sqlite3, or
the environment. That is what makes the behaviour above testable without a
network, a clock, or a filesystem.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from dawatch.errors import NotifyError
from dawatch.metrics import MetricsSink
from dawatch.models import Deviation
from dawatch.notifier import Notifier
from dawatch.sources import DeviationSource
from dawatch.store import SeenStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RunResult:
    fetched: int
    new: int
    notified: int
    errors: int
    seeded: bool

    @property
    def exit_code(self) -> int:
        """0 when everything worked, 1 when any notification failed."""
        return 1 if self.errors else 0


class WatchService:
    """Fetch the feed, notify what is new, remember what was notified."""

    def __init__(
        self,
        source: DeviationSource,
        store: SeenStore,
        notifier: Notifier,
        metrics: MetricsSink,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._store = store
        self._notifier = notifier
        self._metrics = metrics
        self._clock = clock

    def run(
        self,
        date: str | None = None,
        dry_run: bool = False,
        allow_seed: bool = True,
    ) -> RunResult:
        """Perform exactly one poll.

        Args:
            date: Feed day in YYYY-MM-DD. Defaults to today.
            dry_run: Report what would be sent without sending or writing.
            allow_seed: When the store is empty, record the feed as seen
                without notifying. Prevents a first deployment from firing a
                burst of notifications.

        Raises:
            FetchError: if the feed could not be retrieved.
        """
        started = self._clock()

        try:
            deviations = self._fetch(date)
            seeding = allow_seed and not dry_run and self._store.is_empty()

            new = [d for d in deviations if not self._store.has_seen(d.deviationid)]
            self._metrics.record_fetched(len(deviations))

            if seeding:
                self._seed(new)
                return self._finish(
                    started, len(deviations), len(new), 0, 0, seeded=True, dry_run=False
                )

            if dry_run:
                for deviation in new:
                    log.info(
                        "run.would_notify",
                        deviationid=deviation.deviationid,
                        title=deviation.title,
                    )
                return self._finish(
                    started, len(deviations), len(new), 0, 0, seeded=False, dry_run=True
                )

            notified, errors = self._notify_all(new)
            return self._finish(
                started, len(deviations), len(new), notified, errors, seeded=False, dry_run=False
            )
        finally:
            self._metrics.record_duration(self._clock() - started)
            self._metrics.flush()

    def _fetch(self, date: str | None) -> list[Deviation]:
        try:
            return list(self._source.fetch(date))
        except Exception:
            self._metrics.record_error("fetch")
            raise

    def _seed(self, new: list[Deviation]) -> None:
        """Record deviations as seen without notifying."""
        for deviation in new:
            self._store.mark_seen(deviation, notified=False)
        log.info("run.seeded", count=len(new))

    def _notify_all(self, new: list[Deviation]) -> tuple[int, int]:
        """Notify each deviation, marking it seen only once delivery succeeds.

        The ordering is deliberate. Marking seen after a successful send gives
        at-least-once delivery: a crash between the two causes one duplicate
        next run. The reverse would give at-most-once, where the same crash
        loses the deviation permanently and silently.
        """
        notified = 0
        errors = 0

        for deviation in new:
            try:
                self._notifier.send(deviation)
            except NotifyError as exc:
                errors += 1
                self._metrics.record_error("notify")
                log.error(
                    "run.notify_failed",
                    deviationid=deviation.deviationid,
                    error=str(exc),
                )
                continue

            self._store.mark_seen(deviation, notified=True)
            notified += 1

        return notified, errors

    def _finish(
        self,
        started: float,
        fetched: int,
        new: int,
        notified: int,
        errors: int,
        seeded: bool,
        dry_run: bool,
    ) -> RunResult:
        result = RunResult(
            fetched=fetched, new=new, notified=notified, errors=errors, seeded=seeded
        )
        if errors == 0 and not dry_run:
            # Only a clean run resets the staleness clock, so a run that is
            # half-failing does not look healthy to the alert. A dry run did
            # no real work either, so it must not silence the dead-job alarm
            # for the debugging session that is most likely to trigger one.
            self._metrics.record_success(self._clock())

        self._metrics.record_notified(notified)
        log.info(
            "run.complete",
            fetched=fetched,
            new=new,
            notified=notified,
            errors=errors,
            seeded=seeded,
            duration_seconds=round(self._clock() - started, 3),
        )
        return result

"""Metrics.

A CronJob pod has usually exited before Prometheus would notice it, so it
cannot be scraped. Metrics are pushed to a Pushgateway instead, which exists
precisely for batch jobs.
"""

from collections.abc import Callable
from typing import Protocol

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import push_to_gateway as _push_to_gateway

log = structlog.get_logger(__name__)

PushFn = Callable[[str, str, CollectorRegistry], None]


class MetricsSink(Protocol):
    def record_fetched(self, count: int) -> None: ...

    def record_notified(self, count: int) -> None: ...

    def record_error(self, stage: str) -> None: ...

    def record_success(self, timestamp: float) -> None: ...

    def record_duration(self, seconds: float) -> None: ...

    def flush(self) -> None: ...


class NullSink:
    """Records nothing anywhere. Used when metrics are disabled, and in tests."""

    def __init__(self) -> None:
        self.fetched = 0
        self.notified = 0
        self.errors: dict[str, int] = {}
        self.duration: float | None = None
        self.success_timestamp: float | None = None
        self.flushed = False

    def record_fetched(self, count: int) -> None:
        self.fetched += count

    def record_notified(self, count: int) -> None:
        self.notified += count

    def record_error(self, stage: str) -> None:
        self.errors[stage] = self.errors.get(stage, 0) + 1

    def record_success(self, timestamp: float) -> None:
        self.success_timestamp = timestamp

    def record_duration(self, seconds: float) -> None:
        self.duration = seconds

    def flush(self) -> None:
        self.flushed = True


def _default_push(gateway: str, job: str, registry: CollectorRegistry) -> None:
    _push_to_gateway(gateway, job=job, registry=registry)


class PushgatewaySink:
    """Collects metrics for one run and pushes them at the end."""

    def __init__(
        self,
        gateway_url: str,
        job: str = "dawatch",
        push: PushFn = _default_push,
    ) -> None:
        self._gateway_url = gateway_url
        self._job = job
        self._push = push
        self.registry = CollectorRegistry()

        self._fetched = Counter(
            "dawatch_deviations_fetched_total",
            "Deviations returned by the feed",
            registry=self.registry,
        )
        self._notified = Counter(
            "dawatch_notifications_sent_total",
            "Notifications delivered successfully",
            registry=self.registry,
        )
        self._errors = Counter(
            "dawatch_errors_total",
            "Errors by pipeline stage",
            ["stage"],
            registry=self.registry,
        )
        self._last_success = Gauge(
            "dawatch_last_success_timestamp_seconds",
            "Unix timestamp of the last fully successful run",
            registry=self.registry,
        )
        self._duration = Histogram(
            "dawatch_run_duration_seconds",
            "Wall-clock duration of a run",
            registry=self.registry,
        )

    def record_fetched(self, count: int) -> None:
        self._fetched.inc(count)

    def record_notified(self, count: int) -> None:
        self._notified.inc(count)

    def record_error(self, stage: str) -> None:
        self._errors.labels(stage=stage).inc()

    def record_success(self, timestamp: float) -> None:
        self._last_success.set(timestamp)

    def record_duration(self, seconds: float) -> None:
        self._duration.observe(seconds)

    def flush(self) -> None:
        """Push to the gateway, swallowing failures.

        Metrics are diagnostics. An unreachable Pushgateway must never turn a
        successful notification run into a failed one.
        """
        try:
            self._push(self._gateway_url, self._job, self.registry)
            log.debug("metrics.pushed", gateway=self._gateway_url)
        except Exception as exc:
            log.warning("metrics.push_failed", error=str(exc))

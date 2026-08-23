import pytest
import structlog

from dawatch.errors import FetchError
from dawatch.metrics import NullSink
from dawatch.models import Deviation
from dawatch.notifier import RecordingNotifier
from dawatch.service import WatchService
from dawatch.store import InMemoryStore


def make_deviation(deviationid: str) -> Deviation:
    return Deviation.model_validate(
        {
            "deviationid": deviationid,
            "title": f"Art {deviationid}",
            "author": {"username": "artist"},
            "url": f"https://example.invalid/{deviationid}",
        }
    )


class FakeSource:
    def __init__(self, deviations: list[Deviation], error: Exception | None = None) -> None:
        self._deviations = deviations
        self._error = error
        self.calls: list[str | None] = []

    def fetch(self, date: str | None = None) -> list[Deviation]:
        self.calls.append(date)
        if self._error is not None:
            raise self._error
        return list(self._deviations)


def build(
    deviations: list[Deviation],
    *,
    store: InMemoryStore | None = None,
    notifier: RecordingNotifier | None = None,
    source_error: Exception | None = None,
) -> tuple[WatchService, InMemoryStore, RecordingNotifier, NullSink, FakeSource]:
    store = store or InMemoryStore()
    notifier = notifier or RecordingNotifier()
    metrics = NullSink()
    source = FakeSource(deviations, source_error)
    service = WatchService(source, store, notifier, metrics, clock=lambda: 1000.0)
    return service, store, notifier, metrics, source


# --- seeding -------------------------------------------------------------


def test_empty_store_seeds_without_notifying() -> None:
    """A first deployment must not fire twenty notifications at once."""
    service, store, notifier, _, _ = build([make_deviation("A"), make_deviation("B")])

    result = service.run()

    assert result.seeded is True
    assert result.notified == 0
    assert notifier.sent == []
    assert store.has_seen("A") and store.has_seen("B")


def test_seeded_rows_are_not_marked_notified() -> None:
    service, store, _, _, _ = build([make_deviation("A")])

    service.run()

    assert store.notified_at("A") is None


def test_no_seed_notifies_everything_on_an_empty_store() -> None:
    service, _, notifier, _, _ = build([make_deviation("A")])

    result = service.run(allow_seed=False)

    assert result.seeded is False
    assert [d.deviationid for d in notifier.sent] == ["A"]


def test_a_non_empty_store_does_not_seed() -> None:
    store = InMemoryStore()
    store.mark_seen(make_deviation("OLD"), notified=True)
    service, _, notifier, _, _ = build([make_deviation("A")], store=store)

    result = service.run()

    assert result.seeded is False
    assert [d.deviationid for d in notifier.sent] == ["A"]


# --- the normal path -----------------------------------------------------


def seeded_store() -> InMemoryStore:
    store = InMemoryStore()
    store.mark_seen(make_deviation("SEED"), notified=True)
    return store


def test_notifies_only_unseen_deviations() -> None:
    store = seeded_store()
    store.mark_seen(make_deviation("A"), notified=True)
    service, _, notifier, _, _ = build([make_deviation("A"), make_deviation("B")], store=store)

    result = service.run()

    assert [d.deviationid for d in notifier.sent] == ["B"]
    assert result.fetched == 2
    assert result.new == 1
    assert result.notified == 1


def test_notifies_nothing_when_everything_is_seen() -> None:
    store = seeded_store()
    store.mark_seen(make_deviation("A"), notified=True)
    service, _, notifier, _, _ = build([make_deviation("A")], store=store)

    result = service.run()

    assert notifier.sent == []
    assert result.new == 0
    assert result.exit_code == 0


def test_handles_an_empty_feed() -> None:
    service, _, notifier, _, _ = build([], store=seeded_store())

    result = service.run()

    assert notifier.sent == []
    assert result.fetched == 0
    assert result.exit_code == 0


def test_marks_seen_after_a_successful_send() -> None:
    store = seeded_store()
    service, _, _, _, _ = build([make_deviation("A")], store=store)

    service.run()

    assert store.has_seen("A")
    assert store.notified_at("A") is not None


def test_does_not_renotify_on_a_second_run() -> None:
    """The core promise: no deviation is ever notified twice."""
    store = seeded_store()
    notifier = RecordingNotifier()
    deviations = [make_deviation("A")]

    build(deviations, store=store, notifier=notifier)[0].run()
    build(deviations, store=store, notifier=notifier)[0].run()

    assert [d.deviationid for d in notifier.sent] == ["A"]


def test_forwards_the_date_to_the_source() -> None:
    service, _, _, _, source = build([], store=seeded_store())

    service.run(date="2026-08-01")

    assert source.calls == ["2026-08-01"]


# --- failure handling ----------------------------------------------------


def test_a_failed_notification_leaves_the_deviation_unseen() -> None:
    """The other half of the promise: nothing is silently dropped."""
    store = seeded_store()
    notifier = RecordingNotifier(fail_ids={"A"})
    service, _, _, _, _ = build([make_deviation("A")], store=store, notifier=notifier)

    result = service.run()

    assert store.has_seen("A") is False
    assert result.errors == 1
    assert result.exit_code == 1


def test_a_failed_notification_does_not_stop_later_ones() -> None:
    store = seeded_store()
    notifier = RecordingNotifier(fail_ids={"A"})
    service, _, _, _, _ = build(
        [make_deviation("A"), make_deviation("B")], store=store, notifier=notifier
    )

    result = service.run()

    assert [d.deviationid for d in notifier.sent] == ["B"]
    assert store.has_seen("B") is True
    assert result.notified == 1
    assert result.errors == 1


def test_a_failed_deviation_is_retried_next_run() -> None:
    store = seeded_store()
    deviations = [make_deviation("A")]

    failing = RecordingNotifier(fail_ids={"A"})
    build(deviations, store=store, notifier=failing)[0].run()

    working = RecordingNotifier()
    build(deviations, store=store, notifier=working)[0].run()

    assert [d.deviationid for d in working.sent] == ["A"]


def test_a_fetch_failure_propagates() -> None:
    service, _, _, _, _ = build([], store=seeded_store(), source_error=FetchError("down"))

    with pytest.raises(FetchError):
        service.run()


def test_a_fetch_failure_records_a_metric() -> None:
    store = seeded_store()
    metrics = NullSink()
    source = FakeSource([], FetchError("down"))
    service = WatchService(source, store, RecordingNotifier(), metrics, clock=lambda: 1000.0)

    with pytest.raises(FetchError):
        service.run()

    assert metrics.errors == {"fetch": 1}


# --- dry run -------------------------------------------------------------


def test_dry_run_notifies_nothing_and_writes_nothing() -> None:
    store = seeded_store()
    service, _, notifier, _, _ = build([make_deviation("A")], store=store)

    result = service.run(dry_run=True)

    assert notifier.sent == []
    assert store.has_seen("A") is False
    assert result.new == 1


def test_dry_run_does_not_seed_an_empty_store() -> None:
    store = InMemoryStore()
    service, _, _, _, _ = build([make_deviation("A")], store=store)

    service.run(dry_run=True)

    assert store.is_empty() is True


# --- metrics -------------------------------------------------------------


def test_records_metrics_for_a_successful_run() -> None:
    store = seeded_store()
    service, _, _, metrics, _ = build([make_deviation("A")], store=store)

    service.run()

    assert metrics.fetched == 1
    assert metrics.notified == 1
    assert metrics.success_timestamp == 1000.0
    assert metrics.duration is not None
    assert metrics.flushed is True


def test_does_not_record_success_when_a_notification_failed() -> None:
    """The staleness alert must not be reset by a partially failed run."""
    store = seeded_store()
    notifier = RecordingNotifier(fail_ids={"A"})
    service, _, _, metrics, _ = build([make_deviation("A")], store=store, notifier=notifier)

    service.run()

    assert metrics.success_timestamp is None
    assert metrics.errors == {"notify": 1}


def test_flushes_metrics_even_when_fetching_fails() -> None:
    store = seeded_store()
    metrics = NullSink()
    source = FakeSource([], FetchError("down"))
    service = WatchService(source, store, RecordingNotifier(), metrics, clock=lambda: 1000.0)

    with pytest.raises(FetchError):
        service.run()

    assert metrics.flushed is True


# --- logging ---------------------------------------------------------------


def test_a_failed_notification_is_logged_with_diagnostic_context() -> None:
    """A swallowed notify error must still leave a diagnostic trail."""
    store = seeded_store()
    notifier = RecordingNotifier(fail_ids={"A"})
    service, _, _, _, _ = build([make_deviation("A")], store=store, notifier=notifier)

    with structlog.testing.capture_logs() as cap_logs:
        service.run()

    failed_logs = [log for log in cap_logs if log["event"] == "run.notify_failed"]
    assert len(failed_logs) == 1
    assert failed_logs[0]["deviationid"] == "A"
    assert "error" in failed_logs[0]

from prometheus_client import CollectorRegistry

from dawatch.metrics import NullSink, PushgatewaySink


def test_null_sink_records_without_pushing() -> None:
    sink = NullSink()

    sink.record_fetched(5)
    sink.record_notified(2)
    sink.record_error("fetch")
    sink.record_error("notify")
    sink.record_duration(1.5)
    sink.record_success(1000.0)
    sink.flush()

    assert sink.fetched == 5
    assert sink.notified == 2
    assert sink.errors == {"fetch": 1, "notify": 1}
    assert sink.duration == 1.5
    assert sink.success_timestamp == 1000.0
    assert sink.flushed is True


def test_pushgateway_sink_pushes_on_flush() -> None:
    pushes: list[tuple[str, str, CollectorRegistry]] = []

    sink = PushgatewaySink(
        "http://gw:9091",
        push=lambda gateway, job, registry: pushes.append((gateway, job, registry)),
    )
    sink.record_fetched(3)
    sink.flush()

    assert len(pushes) == 1
    assert pushes[0][0] == "http://gw:9091"
    assert pushes[0][1] == "dawatch"


def test_pushgateway_sink_does_not_push_before_flush() -> None:
    pushes: list[object] = []

    sink = PushgatewaySink("http://gw:9091", push=lambda *args: pushes.append(args))
    sink.record_fetched(3)

    assert pushes == []


def test_pushgateway_sink_records_metric_values() -> None:
    sink = PushgatewaySink("http://gw:9091", push=lambda *args: None)

    sink.record_fetched(4)
    sink.record_notified(2)
    sink.record_error("notify")
    sink.record_success(1700.0)

    registry = sink.registry
    assert registry.get_sample_value("dawatch_deviations_fetched_total") == 4.0
    assert registry.get_sample_value("dawatch_notifications_sent_total") == 2.0
    assert registry.get_sample_value("dawatch_errors_total", {"stage": "notify"}) == 1.0
    assert registry.get_sample_value("dawatch_last_success_timestamp_seconds") == 1700.0


def test_a_failing_push_does_not_raise() -> None:
    """Metrics are diagnostics. A dead Pushgateway must not fail the run."""

    def explode(gateway: str, job: str, registry: CollectorRegistry) -> None:
        raise OSError("gateway unreachable")

    sink = PushgatewaySink("http://gw:9091", push=explode)
    sink.record_fetched(1)

    sink.flush()  # must not raise

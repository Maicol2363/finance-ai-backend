import pytest

from finance_ai.core.observability import (
    SafeRegistrationMetrics,
    latency_bucket,
    registration_outcome,
)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (201, "success"),
        (400, "validation_failure"),
        (409, "duplicate_conflict"),
        (413, "validation_failure"),
        (422, "validation_failure"),
        (429, "throttled"),
        (503, "configuration_failure"),
        (500, "unexpected_failure"),
    ],
)
def test_registration_outcome_is_bounded(status_code: int, expected: str) -> None:
    assert registration_outcome(status_code) == expected


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [(0, "le_50ms"), (50, "le_50ms"), (51, "le_100ms"), (5_001, "over_5000ms")],
)
def test_latency_bucket_is_bounded(duration_ms: float, expected: str) -> None:
    assert latency_bucket(duration_ms) == expected


def test_metrics_count_attempt_outcome_and_latency_without_labels_from_input() -> None:
    metrics = SafeRegistrationMetrics()

    metrics.observe(status_code=409, duration_ms=75, correlation_id="safe-test-id")

    assert metrics.snapshot() == {
        "attempts": 1,
        "outcome.duplicate_conflict": 1,
        "latency.le_100ms": 1,
    }

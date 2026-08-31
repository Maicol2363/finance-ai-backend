from collections import Counter
from threading import Lock
from typing import Final, Literal

from finance_ai.core.logging import get_logger

RegistrationOutcome = Literal[
    "success",
    "validation_failure",
    "duplicate_conflict",
    "throttled",
    "configuration_failure",
    "unexpected_failure",
]

_LATENCY_BUCKETS_MS: Final = (50, 100, 250, 500, 1_000, 2_000, 5_000)


def registration_outcome(status_code: int) -> RegistrationOutcome:
    if status_code == 201:
        return "success"
    if status_code == 409:
        return "duplicate_conflict"
    if status_code == 429:
        return "throttled"
    if status_code == 503:
        return "configuration_failure"
    if 400 <= status_code < 500:
        return "validation_failure"
    return "unexpected_failure"


def latency_bucket(duration_ms: float) -> str:
    for upper_bound in _LATENCY_BUCKETS_MS:
        if duration_ms <= upper_bound:
            return f"le_{upper_bound}ms"
    return "over_5000ms"


class SafeRegistrationMetrics:
    """Process-local counters behind a boundary replaceable by hosted telemetry later."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: Counter[str] = Counter()
        self._logger = get_logger(__name__)

    def observe(self, *, status_code: int, duration_ms: float, correlation_id: str) -> None:
        outcome = registration_outcome(status_code)
        bucket = latency_bucket(duration_ms)
        with self._lock:
            self._counters["attempts"] += 1
            self._counters[f"outcome.{outcome}"] += 1
            self._counters[f"latency.{bucket}"] += 1

        self._logger.info(
            "Registration completed",
            extra={
                "correlation_id": correlation_id,
                "duration_ms": duration_ms,
                "event": "registration.completed",
                "outcome": outcome,
                "status": status_code,
            },
        )

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)


registration_metrics = SafeRegistrationMetrics()


AuthOperation = Literal["login", "refresh", "me", "logout"]


class SafeAuthMetrics:
    """Low-cardinality auth counters that never receive identities or credentials."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: Counter[str] = Counter()
        self._logger = get_logger(__name__)

    def observe(
        self,
        *,
        operation: AuthOperation,
        status_code: int,
        duration_ms: float,
        correlation_id: str,
    ) -> None:
        outcome = "success" if status_code < 400 else "failure"
        bucket = latency_bucket(duration_ms)
        with self._lock:
            self._counters[f"{operation}.attempts"] += 1
            self._counters[f"{operation}.outcome.{outcome}"] += 1
            self._counters[f"{operation}.latency.{bucket}"] += 1
        self._logger.info(
            "Authentication operation completed",
            extra={
                "correlation_id": correlation_id,
                "duration_ms": duration_ms,
                "event": "authentication.completed",
                "operation": operation,
                "outcome": outcome,
                "status": status_code,
            },
        )

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)


auth_metrics = SafeAuthMetrics()

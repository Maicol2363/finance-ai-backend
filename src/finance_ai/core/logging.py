import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_SAFE_EXTRA_FIELDS = {
    "correlation_id",
    "duration_ms",
    "error_type",
    "event",
    "operation",
    "outcome",
    "provider",
    "status",
}

# Request-scoped correlation ID published by RequestGuardMiddleware. Carrying it in a context
# variable lets deep layers (provider gateways, application services) be matched to a client
# report without logging raw upstream payloads or exceptions.
_correlation_id: ContextVar[str | None] = ContextVar("finance_ai_correlation_id", default=None)


def bind_correlation_id(value: str | None) -> None:
    """Publishes, or clears, the correlation ID inherited by every log record in this task."""
    _correlation_id.set(value)


def current_correlation_id() -> str | None:
    return _correlation_id.get()


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _SAFE_EXTRA_FIELDS:
            value = record.__dict__.get(key)
            if value is not None:
                payload[key] = value
        if "correlation_id" not in payload:
            inherited = current_correlation_id()
            if inherited is not None:
                payload["correlation_id"] = inherited
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

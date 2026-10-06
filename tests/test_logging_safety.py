import json
import logging

from finance_ai.core.logging import (
    SafeJsonFormatter,
    bind_correlation_id,
    current_correlation_id,
)


def test_structured_formatter_allows_only_safe_context_fields() -> None:
    record = logging.LogRecord(
        name="finance_ai.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Registration completed",
        args=(),
        exc_info=None,
    )
    record.correlation_id = "safe-correlation"
    record.event = "registration.completed"
    record.email = "private@example.com"
    record.password = "do-not-log-me"
    record.password_hash = "$argon2id$private"
    record.request_body = {"password": "do-not-log-me"}

    rendered = SafeJsonFormatter().format(record)
    payload = json.loads(rendered)

    assert payload["correlation_id"] == "safe-correlation"
    assert payload["event"] == "registration.completed"
    assert "private@example.com" not in rendered
    assert "do-not-log-me" not in rendered
    assert "$argon2id$private" not in rendered
    assert "request_body" not in rendered


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="finance_ai.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=20,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_records_inherit_the_request_correlation_id() -> None:
    """Provider and service logs must be matchable to a client report without raw payloads."""
    try:
        bind_correlation_id("request-correlation")
        assert current_correlation_id() == "request-correlation"
        inherited = json.loads(SafeJsonFormatter().format(_record("Provider call finished")))
    finally:
        bind_correlation_id(None)

    assert inherited["correlation_id"] == "request-correlation"
    assert "correlation_id" not in json.loads(SafeJsonFormatter().format(_record("No request")))


def test_explicit_correlation_id_wins_over_the_inherited_one() -> None:
    record = _record("Registration completed")
    record.correlation_id = "explicit-correlation"
    try:
        bind_correlation_id("request-correlation")
        payload = json.loads(SafeJsonFormatter().format(record))
    finally:
        bind_correlation_id(None)

    assert payload["correlation_id"] == "explicit-correlation"

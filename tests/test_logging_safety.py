import json
import logging

from finance_ai.core.logging import SafeJsonFormatter


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

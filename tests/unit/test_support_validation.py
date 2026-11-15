import pytest
from fastapi import Request
from pydantic import ValidationError

from finance_ai.core.errors import AppError
from finance_ai.support.rate_limit import enforce_support_limit, support_limiter
from finance_ai.support.schemas import (
    AgentTicketCreate,
    MessageCreate,
    TicketCreate,
    TransitionRequest,
)


@pytest.mark.parametrize(
    "value", ["", "   ", "x" * 4001, "hello\x00world", "hello\x01world", "\ud800"]
)
@pytest.mark.parametrize("schema,field", [(MessageCreate, "body"), (TransitionRequest, "summary")])
def test_message_and_transition_reject_invalid_text(value, schema, field):
    with pytest.raises(ValidationError):
        schema.model_validate({field: value})


def test_exact_boundaries_and_untrusted_identity_fields():
    assert len(MessageCreate(body="x" * 4000).body) == 4000
    assert TransitionRequest(summary="  Resultado  ").summary == "Resultado"
    assert TicketCreate(subject="x" * 160, category="general", description="x" * 4000)
    for change in (
        {"subject": "x" * 161},
        {"category": "admin"},
        {"description": ""},
        {"customer_user_id": 9},
        {"created_by_user_id": 9},
    ):
        with pytest.raises(ValidationError):
            TicketCreate.model_validate(
                {"subject": "Ayuda", "category": "general", "description": "Texto", **change}
            )
    with pytest.raises(ValidationError):
        AgentTicketCreate(
            subject="Ayuda", category="general", description="Texto", customer_identifier=" "
        )
    with pytest.raises(ValidationError):
        MessageCreate.model_validate({"body": "Text", "created_at": "2026-01-01"})


def test_support_peer_limit_returns_safe_429_and_retry_header():
    support_limiter.cache_clear()
    request = Request({"type": "http", "client": ("support-rate-test", 443)})
    try:
        for _ in range(120):
            enforce_support_limit(request)
        with pytest.raises(AppError) as error:
            enforce_support_limit(request)
        assert error.value.status == 429
        assert error.value.code == "support_rate_limited"
        assert error.value.headers == {"Retry-After": "60"}
    finally:
        support_limiter.cache_clear()

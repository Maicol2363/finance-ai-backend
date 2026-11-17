"""ADM-05 #11: staff-only workflow metadata never reaches the customer API (backend-enforced)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from finance_ai.accounts.dependencies import get_authenticated_principal
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.config import get_settings
from finance_ai.main import create_app
from finance_ai.support.dependencies import get_ticket_workflow
from finance_ai.support.schemas import (
    CustomerMetadata,
    CustomerTicket,
    StaffConversation,
    TicketHistory,
    TicketMessage,
)

NOW = datetime(2026, 11, 15, tzinfo=UTC)
SECRET_REASON = "STAFF-ONLY escalation reason"


def staff_conversation() -> StaffConversation:
    """A deliberately over-rich object, as if a service returned the staff projection."""
    return StaffConversation(
        ticket=CustomerTicket(
            id=3,
            subject="Cannot sign in",
            status="resolved",
            created_at=NOW,
            assigned_staff_user_id=2,
            category="account",
            description="Details",
            resolution_summary="Fixed for you.",
        ),
        messages=[TicketMessage(id=1, author_user_id=2, body="Public reply", created_at=NOW)],
        customer=CustomerMetadata(id=9, email="customer@example.com", is_active=True),
        history=[
            TicketHistory(
                id=1,
                actor_user_id=2,
                event_type="ticket_escalated",
                previous_status="in_progress",
                new_status="escalated",
                previous_assignee_user_id=None,
                new_assignee_user_id=None,
                created_at=NOW,
                internal_reason=SECRET_REASON,
            )
        ],
    )


class StubWorkflow:
    def read(self, actor: int, ticket_id: int, *, staff: bool) -> StaffConversation:
        del actor, ticket_id, staff
        return staff_conversation()

    def list_own(self, actor: int) -> list[CustomerTicket]:
        del actor
        return [staff_conversation().ticket]


@pytest.fixture
def customer_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "test")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_authenticated_principal] = lambda: PublicAuthenticatedUser(
        user_id=9, email="customer@example.com", preferred_currency="COP"
    )
    app.dependency_overrides[get_ticket_workflow] = StubWorkflow
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_customer_conversation_is_projected_to_customer_fields_only(
    customer_client: TestClient,
) -> None:
    response = customer_client.get(
        "/api/v1/support/tickets/3", headers={"Authorization": "Bearer " + "a" * 43}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ticket", "messages", "attachments"}
    assert body["attachments"] == []
    assert body["ticket"]["resolution_summary"] == "Fixed for you."
    assert SECRET_REASON not in response.text
    for staff_only in ("history", "internal_reason", "customer", "email"):
        assert staff_only not in response.text


def test_customer_ticket_list_never_includes_staff_metadata(customer_client: TestClient) -> None:
    response = customer_client.get(
        "/api/v1/support/tickets", headers={"Authorization": "Bearer " + "a" * 43}
    )

    assert response.status_code == 200
    assert SECRET_REASON not in response.text and "internal_reason" not in response.text

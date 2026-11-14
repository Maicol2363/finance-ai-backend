from datetime import UTC, datetime

from fastapi.testclient import TestClient

from finance_ai.core.errors import StaffAuthenticationRequiredError
from finance_ai.main import create_app
from finance_ai.staff_auth.dependencies import (
    get_staff_session_service,
    staff_cookie_names,
)
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.dependencies import get_support_ticket_service
from finance_ai.support.schemas import (
    TicketClaimResult,
    TicketDetail,
    TicketQueueItem,
    TicketStatus,
)


class StubSessionService:
    def resolve(self, raw_access: str | None, *, correlation_id: str) -> StaffSessionUser:
        del correlation_id
        if raw_access != "valid-access":
            raise StaffAuthenticationRequiredError()
        return StaffSessionUser(id=42, email="agent@example.test", role="support_agent")


class StubSupportService:
    status_filter: TicketStatus | None = None

    def list_queue(
        self, user: StaffSessionUser, status: TicketStatus | None
    ) -> list[TicketQueueItem]:
        self.status_filter = status
        return [
            TicketQueueItem(
                id=7,
                subject="Consulta general",
                status=TicketStatus.OPEN,
                created_at=datetime(2026, 11, 12, tzinfo=UTC),
                assigned_staff_user_id=None,
            )
        ]

    def get_assigned(self, user: StaffSessionUser, ticket_id: int) -> TicketDetail:
        del user
        return TicketDetail(
            id=ticket_id,
            subject="Consulta general",
            status=TicketStatus.IN_PROGRESS,
            created_at=datetime(2026, 11, 12, tzinfo=UTC),
            assigned_staff_user_id=42,
            category="general",
            description="Contexto autorizado",
        )

    def claim(
        self, user: StaffSessionUser, ticket_id: int, correlation_id: str
    ) -> TicketClaimResult:
        del user, correlation_id
        return TicketClaimResult(
            id=ticket_id,
            subject="Consulta general",
            status=TicketStatus.IN_PROGRESS,
            created_at=datetime(2026, 11, 12, tzinfo=UTC),
            assigned_staff_user_id=42,
        )


def setup_client() -> tuple[TestClient, StubSupportService]:
    app = create_app()
    service = StubSupportService()
    app.dependency_overrides[get_staff_session_service] = StubSessionService
    app.dependency_overrides[get_support_ticket_service] = lambda: service
    return TestClient(app), service


def test_support_endpoints_require_staff_request_boundary_and_session() -> None:
    client, _ = setup_client()
    assert client.get("/api/v1/staff/support/tickets").status_code == 403
    response = client.get(
        "/api/v1/staff/support/tickets",
        headers={"X-Staff-Request": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 401


def test_queue_response_contains_only_the_limited_projection() -> None:
    client, service = setup_client()
    access_name, _ = staff_cookie_names()
    client.cookies.set(access_name, "valid-access")
    response = client.get(
        "/api/v1/staff/support/tickets?status=open",
        headers={"X-Staff-Request": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    assert response.json()[0] == {
        "id": 7,
        "subject": "Consulta general",
        "status": "open",
        "created_at": "2026-11-12T00:00:00Z",
        "assigned_staff_user_id": None,
    }
    assert service.status_filter is TicketStatus.OPEN


def test_invalid_status_filter_is_rejected() -> None:
    client, _ = setup_client()
    access_name, _ = staff_cookie_names()
    client.cookies.set(access_name, "valid-access")
    response = client.get(
        "/api/v1/staff/support/tickets?status=unknown",
        headers={"X-Staff-Request": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 422

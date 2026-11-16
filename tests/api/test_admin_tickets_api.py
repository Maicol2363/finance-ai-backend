"""HTTP contract, permission matrix and input bounds of ADM-05."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from finance_ai.admin import dependencies as admin_dependencies
from finance_ai.admin.dependencies import get_admin_ticket_service
from finance_ai.admin.schemas import (
    AdminCase,
    EscalatedTicketItem,
    EscalationOrigin,
    ReturnTicketResult,
)
from finance_ai.core.config import get_settings
from finance_ai.core.errors import (
    EscalationClaimConflictError,
    SupportTicketNotFoundError,
    SupportTicketStateConflictError,
)
from finance_ai.main import create_app
from finance_ai.staff_auth.dependencies import get_access_denial_recorder, get_staff_access_user
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.schemas import CustomerMetadata, CustomerTicket

ORIGIN = "https://staff.example.test"
HEADERS = {"Origin": ORIGIN, "X-Staff-Request": "1"}
NOW = datetime(2026, 11, 15, tzinfo=UTC)


def case() -> AdminCase:
    return AdminCase(
        ticket=CustomerTicket(
            id=3,
            subject="Cannot sign in",
            status="escalated",
            created_at=NOW,
            assigned_staff_user_id=2,
            category="account",
            description="Details",
            resolution_summary=None,
        ),
        customer=CustomerMetadata(id=9, email="customer@example.com", is_active=True),
        messages=[],
        history=[],
        escalation_reason="needs an administrator",
        owner_admin_id=1,
        owned_by_me=True,
        originating_agent=EscalationOrigin(user_id=2, can_receive_return=True),
    )


class StubTickets:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failure: Exception | None = None

    def _run(self, name: str, kwargs: dict[str, object]) -> None:
        self.calls.append((name, kwargs))
        if self.failure:
            raise self.failure

    def list_escalated(self, **kwargs: object) -> list[EscalatedTicketItem]:
        self._run("list", kwargs)
        return [
            EscalatedTicketItem(
                id=3,
                subject="Cannot sign in",
                category="account",
                created_at=NOW,
                owner_admin_id=None,
                originating_agent_id=2,
                owned_by_me=False,
            )
        ]

    def get_case(self, **kwargs: object) -> AdminCase:
        self._run("case", kwargs)
        return case()

    def claim(self, **kwargs: object) -> AdminCase:
        self._run("claim", kwargs)
        return case()

    def reply(self, **kwargs: object) -> None:
        self._run("reply", kwargs)

    def resolve(self, **kwargs: object) -> None:
        self._run("resolve", kwargs)

    def return_to_support(self, **kwargs: object) -> ReturnTicketResult:
        self._run("return", kwargs)
        return ReturnTicketResult(status="in_progress", assigned_staff_user_id=2)


@dataclass
class Recorder:
    records: list[dict[str, object]] = field(default_factory=list)

    def record(self, **kwargs: object) -> None:
        self.records.append(kwargs)


@dataclass
class Harness:
    client: TestClient
    service: StubTickets
    recorder: Recorder
    principal: dict[str, StaffSessionUser]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", f'["{ORIGIN}"]')
    get_settings.cache_clear()
    admin_dependencies._limiter.cache_clear()
    app = create_app()
    service, recorder = StubTickets(), Recorder()
    principal = {"user": StaffSessionUser(id=1, email="admin@example.test", role="admin")}
    app.dependency_overrides[get_staff_access_user] = lambda: principal["user"]
    app.dependency_overrides[get_access_denial_recorder] = lambda: recorder
    app.dependency_overrides[get_admin_ticket_service] = lambda: service
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        yield Harness(client, service, recorder, principal)
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    admin_dependencies._limiter.cache_clear()


BASE = "/api/v1/staff/admin/tickets"
ENDPOINTS = [
    ("get", f"{BASE}/escalated", None),
    ("get", f"{BASE}/3", None),
    ("post", f"{BASE}/3/claim", {}),
    ("post", f"{BASE}/3/messages", {"body": "hello"}),
    ("post", f"{BASE}/3/resolve", {"summary": "done"}),
    ("post", f"{BASE}/3/return", {"reason": "back to support"}),
]


def send(h: Harness, method: str, url: str, body: dict[str, object] | None):  # type: ignore[no-untyped-def]
    if method == "get":
        return h.client.get(url, headers=HEADERS)
    return h.client.post(url, json=body, headers=HEADERS)


@pytest.mark.parametrize("role", ["user", "support_agent", "security_auditor"])
@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS)
def test_only_administrators_can_use_escalation_management(
    harness: Harness, role: str, method: str, url: str, body: dict[str, object] | None
) -> None:
    harness.principal["user"] = StaffSessionUser(id=9, email="x@example.test", role=role)

    response = send(harness, method, url, body)

    assert response.status_code == 403 and response.json()["code"] == "staff_access_denied"
    assert harness.service.calls == []
    assert harness.recorder.records[-1]["resource"] == "administration"


@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS)
def test_unauthenticated_requests_never_reach_the_service(
    harness: Harness, method: str, url: str, body: dict[str, object] | None
) -> None:
    harness.client.app.dependency_overrides.pop(get_staff_access_user)  # type: ignore[attr-defined]

    response = send(harness, method, url, body)

    assert response.status_code == 401 and harness.service.calls == []


def test_queue_is_bounded_and_scoped(harness: Harness) -> None:
    ok = harness.client.get(f"{BASE}/escalated?scope=mine&limit=100", headers=HEADERS)
    too_many = harness.client.get(f"{BASE}/escalated?limit=101", headers=HEADERS)
    bad_scope = harness.client.get(f"{BASE}/escalated?scope=everything", headers=HEADERS)

    assert ok.status_code == 200 and ok.json()[0]["owned_by_me"] is False
    assert too_many.status_code == 422 and bad_scope.status_code == 422
    assert harness.service.calls == [("list", {"admin_id": 1, "scope": "mine", "limit": 100})]


def test_case_view_exposes_only_the_case_context(harness: Harness) -> None:
    response = harness.client.get(f"{BASE}/3", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "ticket",
        "customer",
        "messages",
        "history",
        "escalation_reason",
        "owner_admin_id",
        "owned_by_me",
        "originating_agent",
    }
    assert set(body["customer"]) == {"id", "email", "is_active"}
    assert response.headers["cache-control"] == "no-store"
    rendered = response.text.lower()
    for forbidden in ("password", "balance", "receipt", "token", "amount"):
        assert forbidden not in rendered


def test_mutations_use_the_server_derived_administrator(harness: Harness) -> None:
    assert harness.client.post(f"{BASE}/3/claim", json={}, headers=HEADERS).status_code == 200
    assert (
        harness.client.post(
            f"{BASE}/3/messages", json={"body": "  We are on it  "}, headers=HEADERS
        ).status_code
        == 204
    )
    assert (
        harness.client.post(
            f"{BASE}/3/resolve", json={"summary": "  Fixed  "}, headers=HEADERS
        ).status_code
        == 204
    )
    returned = harness.client.post(
        f"{BASE}/3/return", json={"reason": " handed back "}, headers=HEADERS
    )

    assert returned.status_code == 200
    assert returned.json() == {"status": "in_progress", "assigned_staff_user_id": 2}
    names = [name for name, _ in harness.service.calls]
    assert names == ["claim", "reply", "resolve", "return"]
    assert all(kwargs["admin_id"] == 1 for _name, kwargs in harness.service.calls)
    assert harness.service.calls[1][1]["body"] == "We are on it"
    assert harness.service.calls[2][1]["summary"] == "Fixed"
    assert harness.service.calls[3][1]["reason"] == "handed back"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("messages", {}),
        ("messages", {"body": "   "}),
        ("messages", {"body": "x" * 4001}),
        ("messages", {"body": "ok", "internal": True}),
        ("resolve", {}),
        ("resolve", {"summary": ""}),
        ("resolve", {"summary": "ok", "status": "resolved"}),
        ("return", {}),
        ("return", {"reason": "   "}),
        ("return", {"reason": "ok", "assignee": 5}),
        ("claim", {"admin_owner_user_id": 9}),
    ],
)
def test_blank_oversized_or_extra_fields_are_rejected_before_any_service_call(
    harness: Harness, path: str, body: dict[str, object]
) -> None:
    response = harness.client.post(f"{BASE}/3/{path}", json=body, headers=HEADERS)

    assert response.status_code == 422
    assert harness.service.calls == []


@pytest.mark.parametrize("ticket_id", ["0", "-1", "abc"])
def test_ticket_ids_are_validated(harness: Harness, ticket_id: str) -> None:
    assert harness.client.get(f"{BASE}/{ticket_id}", headers=HEADERS).status_code == 422
    assert harness.service.calls == []


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (EscalationClaimConflictError(), 409, "escalation_claim_conflict"),
        (SupportTicketStateConflictError(), 409, "support_ticket_state_conflict"),
        (SupportTicketNotFoundError(), 404, "support_ticket_not_found"),
    ],
)
def test_domain_failures_use_the_shared_problem_contract(
    harness: Harness, failure: Exception, status: int, code: str
) -> None:
    harness.service.failure = failure

    response = harness.client.post(f"{BASE}/3/claim", json={}, headers=HEADERS)

    assert response.status_code == status
    body = response.json()
    assert body["code"] == code and body["correlation_id"]

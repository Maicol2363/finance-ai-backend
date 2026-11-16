"""HTTP contract, permission matrix and input bounds of ADM-01 and ADM-02."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from finance_ai.admin import dependencies as admin_dependencies
from finance_ai.admin.accounts_service import normalize_lookup
from finance_ai.admin.dependencies import get_admin_account_service
from finance_ai.admin.schemas import AdminAccount
from finance_ai.core.config import get_settings
from finance_ai.core.errors import (
    AdminAccountNotFoundError,
    AdminSelfActionError,
    LastActiveAdminError,
)
from finance_ai.main import create_app
from finance_ai.staff_auth.dependencies import get_access_denial_recorder, get_staff_access_user
from finance_ai.staff_auth.schemas import StaffSessionUser

ORIGIN = "https://staff.example.test"
HEADERS = {"Origin": ORIGIN, "X-Staff-Request": "1"}
REGISTERED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def account(account_id: int = 7, status: str = "active") -> AdminAccount:
    return AdminAccount(
        id=account_id,
        email="customer@example.test",
        role="user",
        status=status,  # type: ignore[arg-type]
        registered_at=REGISTERED,
    )


class StubService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failure: Exception | None = None

    def lookup(self, *, email: str | None, account_id: int | None) -> AdminAccount:
        key = normalize_lookup(email, account_id)
        self.calls.append(("lookup", {"key": key}))
        if self.failure:
            raise self.failure
        return account()

    def detail(self, account_id: int) -> AdminAccount:
        self.calls.append(("detail", {"id": account_id}))
        if self.failure:
            raise self.failure
        return account(account_id)

    def suspend(self, **kwargs: object) -> AdminAccount:
        self.calls.append(("suspend", kwargs))
        if self.failure:
            raise self.failure
        return account(status="suspended")

    def reactivate(self, **kwargs: object) -> AdminAccount:
        self.calls.append(("reactivate", kwargs))
        if self.failure:
            raise self.failure
        return account()


@dataclass
class Recorder:
    records: list[dict[str, object]] = field(default_factory=list)

    def record(self, **kwargs: object) -> None:
        self.records.append(kwargs)


@dataclass
class Harness:
    client: TestClient
    service: StubService
    recorder: Recorder
    principal: dict[str, StaffSessionUser]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", f'["{ORIGIN}"]')
    get_settings.cache_clear()
    admin_dependencies._limiter.cache_clear()
    app = create_app()
    service, recorder = StubService(), Recorder()
    principal = {"user": StaffSessionUser(id=1, email="admin@example.test", role="admin")}
    app.dependency_overrides[get_staff_access_user] = lambda: principal["user"]
    app.dependency_overrides[get_access_denial_recorder] = lambda: recorder
    app.dependency_overrides[get_admin_account_service] = lambda: service
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        yield Harness(client, service, recorder, principal)
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    admin_dependencies._limiter.cache_clear()


ENDPOINTS = [
    ("post", "/api/v1/staff/admin/accounts/lookup", {"id": 7}),
    ("get", "/api/v1/staff/admin/accounts/7", None),
    ("post", "/api/v1/staff/admin/accounts/7/suspend", {"reason": "fraud", "confirm": True}),
    ("post", "/api/v1/staff/admin/accounts/7/reactivate", {"confirm": True}),
]


def send(harness: Harness, method: str, url: str, body: dict[str, object] | None):  # type: ignore[no-untyped-def]
    if method == "get":
        return harness.client.get(url, headers=HEADERS)
    return harness.client.post(url, json=body, headers=HEADERS)


@pytest.mark.parametrize("role", ["user", "support_agent", "security_auditor"])
@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS)
def test_every_non_admin_role_is_denied_and_the_denial_is_recorded(
    harness: Harness, role: str, method: str, url: str, body: dict[str, object] | None
) -> None:
    harness.principal["user"] = StaffSessionUser(id=9, email="x@example.test", role=role)

    response = send(harness, method, url, body)

    assert response.status_code == 403
    assert response.json()["code"] == "staff_access_denied"
    assert harness.service.calls == []
    assert harness.recorder.records[-1]["resource"] == "administration"
    assert harness.recorder.records[-1]["actor_id"] == 9


@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS)
def test_unauthenticated_requests_are_rejected_before_any_service_runs(
    harness: Harness, method: str, url: str, body: dict[str, object] | None
) -> None:
    harness.client.app.dependency_overrides.pop(get_staff_access_user)  # type: ignore[attr-defined]

    response = send(harness, method, url, body)

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert harness.service.calls == []


def test_requests_without_the_staff_boundary_header_are_refused(harness: Harness) -> None:
    harness.client.app.dependency_overrides.pop(get_staff_access_user)  # type: ignore[attr-defined]

    response = harness.client.get("/api/v1/staff/admin/accounts/7", headers={"Origin": ORIGIN})

    assert response.status_code == 403
    assert harness.service.calls == []


LOOKUP = "/api/v1/staff/admin/accounts/lookup"


def test_admin_lookup_by_email_and_by_id_returns_the_allowlisted_projection(
    harness: Harness,
) -> None:
    by_email = harness.client.post(LOOKUP, json={"email": "Customer@Example.test"}, headers=HEADERS)
    by_id = harness.client.post(LOOKUP, json={"id": 7}, headers=HEADERS)

    assert by_email.status_code == by_id.status_code == 200
    body = by_email.json()
    assert set(body) == {
        "id",
        "email",
        "role",
        "status",
        "registered_at",
        "account_type",
        "suspension",
    }
    assert body["account_type"] == "login_account"
    assert by_email.headers["cache-control"] == "no-store"
    assert harness.service.calls[0][1]["key"] == ("customer@example.test", None)
    assert harness.service.calls[1][1]["key"] == (None, 7)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "admin_request_invalid"),
        ({"email": "a@example.test", "id": 7}, "admin_request_invalid"),
        ({"email": "not-an-email"}, "admin_request_invalid"),
        ({"id": 0}, "request_invalid"),
        ({"id": "abc"}, "request_invalid"),
        ({"id": 99999999999999999999}, "request_invalid"),
        ({"email": "a@example.test", "role": "admin"}, "request_invalid"),
        ({"email": "x" * 321}, "request_invalid"),
    ],
)
def test_lookup_rejects_missing_conflicting_or_malformed_keys(
    harness: Harness, body: dict[str, object], code: str
) -> None:
    response = harness.client.post(LOOKUP, json=body, headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["code"] == code


def test_an_email_in_a_url_is_never_accepted_so_proxies_cannot_log_it(harness: Harness) -> None:
    response = harness.client.get(f"{LOOKUP}?email=customer@example.test", headers=HEADERS)

    assert response.status_code in {405, 422}
    assert harness.service.calls == []


@pytest.mark.parametrize("account_id", ["0", "-5", "abc", "1.5"])
def test_path_ids_are_validated(harness: Harness, account_id: str) -> None:
    response = harness.client.get(f"/api/v1/staff/admin/accounts/{account_id}", headers=HEADERS)

    assert response.status_code == 422
    assert harness.service.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": "fraud"},
        {"reason": "fraud", "confirm": False},
        {"reason": "   ", "confirm": True},
        {"reason": "", "confirm": True},
        {"reason": "x" * 501, "confirm": True},
        {"reason": "bad\x00char", "confirm": True},
        {"reason": "fraud", "confirm": True, "role": "admin"},
        {"reason": "fraud", "confirm": True, "is_active": True},
    ],
)
def test_suspension_requires_confirmation_a_clean_reason_and_no_extra_fields(
    harness: Harness, body: dict[str, object]
) -> None:
    response = harness.client.post(
        "/api/v1/staff/admin/accounts/7/suspend", json=body, headers=HEADERS
    )

    assert response.status_code == 422
    assert harness.service.calls == []


def test_suspension_passes_the_trimmed_reason_and_server_derived_actor(harness: Harness) -> None:
    response = harness.client.post(
        "/api/v1/staff/admin/accounts/7/suspend",
        json={"reason": "  repeated chargebacks  ", "confirm": True},
        headers={**HEADERS, "X-Correlation-ID": "corr-123"},
    )

    assert response.status_code == 200 and response.json()["status"] == "suspended"
    name, kwargs = harness.service.calls[0]
    assert name == "suspend"
    assert kwargs["actor_id"] == 1 and kwargs["target_id"] == 7
    assert kwargs["reason"] == "repeated chargebacks"
    assert kwargs["correlation_id"] == "corr-123"


def test_reactivation_requires_explicit_confirmation(harness: Harness) -> None:
    refused = harness.client.post(
        "/api/v1/staff/admin/accounts/7/reactivate", json={"confirm": False}, headers=HEADERS
    )
    accepted = harness.client.post(
        "/api/v1/staff/admin/accounts/7/reactivate", json={"confirm": True}, headers=HEADERS
    )

    assert refused.status_code == 422 and accepted.status_code == 200
    assert [name for name, _ in harness.service.calls] == ["reactivate"]


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (AdminSelfActionError(), 409, "admin_self_action_denied"),
        (LastActiveAdminError(), 409, "last_active_admin_protected"),
        (AdminAccountNotFoundError(), 404, "account_not_found"),
    ],
)
def test_domain_failures_use_the_shared_problem_contract(
    harness: Harness, failure: Exception, status: int, code: str
) -> None:
    harness.service.failure = failure

    response = harness.client.post(
        "/api/v1/staff/admin/accounts/7/suspend",
        json={"reason": "fraud", "confirm": True},
        headers=HEADERS,
    )

    assert response.status_code == status
    body = response.json()
    assert body["code"] == code and body["correlation_id"] and body["detail"]
    assert "Traceback" not in response.text


def test_post_requires_a_json_content_type(harness: Harness) -> None:
    response = harness.client.post(
        "/api/v1/staff/admin/accounts/7/suspend",
        content='{"reason":"x","confirm":true}',
        headers={**HEADERS, "Content-Type": "text/plain"},
    )

    assert response.status_code == 415
    assert harness.service.calls == []


def test_lookups_are_rate_limited_per_administrator(harness: Harness) -> None:
    statuses = [
        harness.client.post(LOOKUP, json={"id": 7}, headers=HEADERS).status_code for _ in range(61)
    ]

    assert statuses[:60] == [200] * 60
    assert statuses[60] == 429

"""HTTP contract and permission matrix of ADM-03 (invitation, activation) and ADM-04 (roles)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from finance_ai.admin import dependencies as admin_dependencies
from finance_ai.admin.dependencies import get_admin_staff_service
from finance_ai.admin.schemas import StaffAccountView, StaffInvitationCreated
from finance_ai.core.config import get_settings
from finance_ai.core.errors import (
    AdminSelfActionError,
    RoleChangeNotAllowedError,
    StaffActivationInvalidError,
    StaffEmailUnavailableError,
)
from finance_ai.main import create_app
from finance_ai.staff_auth import dependencies as staff_dependencies
from finance_ai.staff_auth.dependencies import (
    get_access_denial_recorder,
    get_staff_access_user,
    get_staff_activation_service,
)
from finance_ai.staff_auth.schemas import StaffSessionUser

ORIGIN = "https://staff.example.test"
HEADERS = {"Origin": ORIGIN, "X-Staff-Request": "1"}
TOKEN = "t" * 43
SECRET_PASSWORD = "a-long-and-unusual-passphrase"


class StubStaffService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failure: Exception | None = None

    def create_invitation(self, **kwargs: object) -> StaffInvitationCreated:
        self.calls.append(("invite", kwargs))
        if self.failure:
            raise self.failure
        return StaffInvitationCreated(
            invitation_id=3,
            email="new@example.com",
            role="support_agent",
            expires_at=datetime.now(UTC) + timedelta(hours=48),
            activation_token=TOKEN,
        )

    def view(self, staff_id: int) -> StaffAccountView:
        self.calls.append(("view", {"id": staff_id}))
        return view("support_agent")

    def change_role(self, **kwargs: object) -> StaffAccountView:
        self.calls.append(("role", kwargs))
        if self.failure:
            raise self.failure
        return view("security_auditor")


def view(role: str) -> StaffAccountView:
    return StaffAccountView(
        id=5,
        email="staff@example.com",
        role=role,
        status="active",
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
        assignable_roles=["support_agent" if role == "security_auditor" else "security_auditor"],
    )


class StubActivation:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.failure: Exception | None = None

    def activate(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure


@dataclass
class Recorder:
    records: list[dict[str, object]] = field(default_factory=list)

    def record(self, **kwargs: object) -> None:
        self.records.append(kwargs)


@dataclass
class Harness:
    client: TestClient
    staff: StubStaffService
    activation: StubActivation
    recorder: Recorder
    principal: dict[str, StaffSessionUser]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", f'["{ORIGIN}"]')
    get_settings.cache_clear()
    admin_dependencies._limiter.cache_clear()
    staff_dependencies._mfa_limiter.cache_clear()
    app = create_app()
    staff, activation, recorder = StubStaffService(), StubActivation(), Recorder()
    principal = {"user": StaffSessionUser(id=1, email="admin@example.test", role="admin")}
    app.dependency_overrides[get_staff_access_user] = lambda: principal["user"]
    app.dependency_overrides[get_access_denial_recorder] = lambda: recorder
    app.dependency_overrides[get_admin_staff_service] = lambda: staff
    app.dependency_overrides[get_staff_activation_service] = lambda: activation
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        yield Harness(client, staff, activation, recorder, principal)
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    admin_dependencies._limiter.cache_clear()
    staff_dependencies._mfa_limiter.cache_clear()


ADMIN_ENDPOINTS = [
    (
        "post",
        "/api/v1/staff/admin/staff/invitations",
        {"email": "n@example.com", "role": "support_agent"},
    ),
    ("get", "/api/v1/staff/admin/staff/5", None),
    ("post", "/api/v1/staff/admin/staff/5/role", {"role": "security_auditor", "confirm": True}),
]


def send(harness: Harness, method: str, url: str, body: dict[str, object] | None):  # type: ignore[no-untyped-def]
    if method == "get":
        return harness.client.get(url, headers=HEADERS)
    return harness.client.post(url, json=body, headers=HEADERS)


@pytest.mark.parametrize("role", ["user", "support_agent", "security_auditor"])
@pytest.mark.parametrize(("method", "url", "body"), ADMIN_ENDPOINTS)
def test_non_admin_roles_cannot_use_staff_administration(
    harness: Harness, role: str, method: str, url: str, body: dict[str, object] | None
) -> None:
    harness.principal["user"] = StaffSessionUser(id=9, email="x@example.test", role=role)

    response = send(harness, method, url, body)

    assert response.status_code == 403 and response.json()["code"] == "staff_access_denied"
    assert harness.staff.calls == []
    assert harness.recorder.records[-1]["resource"] == "administration"


def test_invitation_returns_the_one_time_token_once_without_caching(harness: Harness) -> None:
    response = harness.client.post(
        "/api/v1/staff/admin/staff/invitations",
        json={"email": "New@Example.com", "role": "support_agent"},
        headers={**HEADERS, "X-Correlation-ID": "corr-9"},
    )

    assert response.status_code == 201
    assert response.json()["activation_token"] == TOKEN
    assert response.headers["cache-control"] == "no-store"
    name, kwargs = harness.staff.calls[0]
    assert name == "invite" and kwargs["actor_id"] == 1 and kwargs["correlation_id"] == "corr-9"


@pytest.mark.parametrize(
    "body",
    [
        {"email": "n@example.com", "role": "admin"},
        {"email": "n@example.com", "role": "user"},
        {"email": "n@example.com", "role": "ADMIN"},
        {"email": "n@example.com", "role": "support_agent", "is_admin": True},
        {"email": "n@example.com", "role": "support_agent", "password": "chosen-by-admin"},
        {"email": "n@example.com"},
        {"role": "support_agent"},
        {"email": "", "role": "support_agent"},
    ],
)
def test_forged_roles_hidden_fields_and_admin_chosen_passwords_are_rejected(
    harness: Harness, body: dict[str, object]
) -> None:
    response = harness.client.post(
        "/api/v1/staff/admin/staff/invitations", json=body, headers=HEADERS
    )

    assert response.status_code == 422
    assert harness.staff.calls == []


def test_duplicate_staff_email_is_a_safe_conflict(harness: Harness) -> None:
    harness.staff.failure = StaffEmailUnavailableError()

    response = harness.client.post(
        "/api/v1/staff/admin/staff/invitations",
        json={"email": "n@example.com", "role": "support_agent"},
        headers=HEADERS,
    )

    assert response.status_code == 409 and response.json()["code"] == "staff_email_unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {"role": "admin", "confirm": True},
        {"role": "user", "confirm": True},
        {"role": "support_agent"},
        {"role": "support_agent", "confirm": False},
        {"role": "support_agent", "confirm": True, "role_id": 2},
    ],
)
def test_role_change_needs_confirmation_and_only_the_two_staff_roles(
    harness: Harness, body: dict[str, object]
) -> None:
    response = harness.client.post("/api/v1/staff/admin/staff/5/role", json=body, headers=HEADERS)

    assert response.status_code == 422
    assert harness.staff.calls == []


def test_role_change_uses_the_server_derived_actor(harness: Harness) -> None:
    response = harness.client.post(
        "/api/v1/staff/admin/staff/5/role",
        json={"role": "security_auditor", "confirm": True},
        headers=HEADERS,
    )

    assert response.status_code == 200 and response.json()["role"] == "security_auditor"
    _name, kwargs = harness.staff.calls[0]
    assert kwargs["actor_id"] == 1 and kwargs["staff_id"] == 5
    assert kwargs["new_role"] == "security_auditor"


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (AdminSelfActionError(), 409, "admin_self_action_denied"),
        (RoleChangeNotAllowedError(), 409, "role_change_not_allowed"),
    ],
)
def test_role_change_failures_use_the_problem_contract(
    harness: Harness, failure: Exception, status: int, code: str
) -> None:
    harness.staff.failure = failure

    response = harness.client.post(
        "/api/v1/staff/admin/staff/5/role",
        json={"role": "support_agent", "confirm": True},
        headers=HEADERS,
    )

    assert response.status_code == status and response.json()["code"] == code


# --- public activation --------------------------------------------------------------------------


def activation_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "token": TOKEN,
        "password": SECRET_PASSWORD,
        "password_confirmation": SECRET_PASSWORD,
    }
    body.update(overrides)
    return body


def test_activation_returns_204_without_a_session_or_any_secret(harness: Harness) -> None:
    response = harness.client.post(
        "/api/v1/staff/auth/activate", json=activation_body(), headers=HEADERS
    )

    assert response.status_code == 204 and response.content == b""
    assert not response.headers.get_list("set-cookie")
    assert response.headers["cache-control"] == "no-store"
    assert harness.activation.calls[0]["token"] == TOKEN


def test_activation_failure_is_one_generic_problem_that_never_echoes_secrets(
    harness: Harness,
) -> None:
    harness.activation.failure = StaffActivationInvalidError()

    response = harness.client.post(
        "/api/v1/staff/auth/activate", json=activation_body(), headers=HEADERS
    )

    assert response.status_code == 400 and response.json()["code"] == "activation_invalid"
    assert TOKEN not in response.text and SECRET_PASSWORD not in response.text


@pytest.mark.parametrize(
    "body",
    [
        activation_body(role="admin"),
        activation_body(token=""),
        activation_body(password=""),
        {"token": TOKEN, "password": SECRET_PASSWORD},
    ],
)
def test_activation_rejects_forged_fields_and_missing_values(
    harness: Harness, body: dict[str, object]
) -> None:
    response = harness.client.post("/api/v1/staff/auth/activate", json=body, headers=HEADERS)

    assert response.status_code == 422
    assert harness.activation.calls == []
    assert SECRET_PASSWORD not in response.text


def test_activation_requires_the_staff_request_boundary(harness: Harness) -> None:
    response = harness.client.post(
        "/api/v1/staff/auth/activate", json=activation_body(), headers={"Origin": ORIGIN}
    )

    assert response.status_code == 403 and harness.activation.calls == []


def test_activation_is_rate_limited(harness: Harness) -> None:
    statuses = [
        harness.client.post(
            "/api/v1/staff/auth/activate", json=activation_body(), headers=HEADERS
        ).status_code
        for _ in range(11)
    ]

    assert statuses[:10] == [204] * 10 and statuses[10] == 429


def test_public_registration_cannot_select_a_staff_or_privileged_role(harness: Harness) -> None:
    response = harness.client.post(
        "/api/v1/auth/register",
        json={
            "email": "sneaky@example.com",
            "password": SECRET_PASSWORD,
            "password_confirmation": SECRET_PASSWORD,
            "role": "admin",
        },
    )

    assert response.status_code == 422
    assert SECRET_PASSWORD not in response.text

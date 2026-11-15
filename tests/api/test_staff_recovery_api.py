"""HTTP contract of SEC-01 recovery codes and authenticator reset."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from finance_ai.auth.dependencies import get_session_rate_limiter
from finance_ai.core.config import get_settings
from finance_ai.core.errors import MfaRecoveryInvalidError, PasswordConfirmationInvalidError
from finance_ai.main import create_app
from finance_ai.staff_auth import dependencies as staff_dependencies
from finance_ai.staff_auth.dependencies import (
    get_staff_access_user,
    get_staff_recovery_service,
    staff_mfa_cookie_name,
)
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.staff_auth.service import StaffMfaChallengeIssued

ORIGIN = "https://staff.example.test"
HEADERS = {"Origin": ORIGIN, "X-Staff-Request": "1"}
CHALLENGE = "c" * 43
CODES = [f"AAAA-BBBB-CCCC-DDD{letter}" for letter in "ABCDEFGHIJ"]


class StubRateLimiter:
    def check_login(self, *, client_key: str, normalized_email: str) -> None:
        del client_key, normalized_email

    def check_refresh(self, *, client_key: str) -> None:
        del client_key


class StubRecovery:
    def __init__(self) -> None:
        self.reset_calls: list[dict[str, object]] = []

    def reset_authenticator(self, **kwargs):  # type: ignore[no-untyped-def]
        self.reset_calls.append(kwargs)
        if kwargs["password"] != "correct-password":
            raise MfaRecoveryInvalidError()
        return StaffMfaChallengeIssued(
            raw_challenge=CHALLENGE,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            enrollment_required=True,
        )

    def issue_recovery_codes(self, user_id, password, *, correlation_id):  # type: ignore[no-untyped-def]
        del user_id, correlation_id
        if password != "correct-password":
            raise PasswordConfirmationInvalidError()
        return CODES


@pytest.fixture
def recovery_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, StubRecovery]]:
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", f'["{ORIGIN}"]')
    get_settings.cache_clear()
    staff_dependencies._mfa_limiter.cache_clear()
    app = create_app()
    service = StubRecovery()
    app.dependency_overrides[get_staff_recovery_service] = lambda: service
    app.dependency_overrides[get_session_rate_limiter] = StubRateLimiter
    app.dependency_overrides[get_staff_access_user] = lambda: StaffSessionUser(
        id=1, email="admin@example.test", role="admin"
    )
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        yield client, service
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_reset_returns_202_with_only_an_httponly_challenge_cookie(
    recovery_client: tuple[TestClient, StubRecovery],
) -> None:
    client, service = recovery_client

    response = client.post(
        "/api/v1/staff/auth/mfa/reset",
        json={
            "email": "admin@example.test",
            "password": "correct-password",
            "recovery_code": "AAAA-BBBB-CCCC-DDDD",
        },
        headers=HEADERS,
    )

    assert response.status_code == 202
    assert response.json()["enrollment_required"] is True
    assert CHALLENGE not in response.text and "correct-password" not in response.text
    set_cookies = response.headers.get_list("set-cookie")
    assert any(cookie.startswith(f"{staff_mfa_cookie_name()}=") for cookie in set_cookies)
    assert all("HttpOnly" in cookie for cookie in set_cookies)
    assert service.reset_calls[0]["recovery_code"] == "AAAA-BBBB-CCCC-DDDD"
    assert service.reset_calls[0]["totp_code"] is None


def test_reset_failure_is_the_generic_problem_without_echoing_secrets(
    recovery_client: tuple[TestClient, StubRecovery],
) -> None:
    client, _ = recovery_client

    response = client.post(
        "/api/v1/staff/auth/mfa/reset",
        json={"email": "admin@example.test", "password": "nope-nope", "code": "123456"},
        headers=HEADERS,
    )

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "mfa_recovery_invalid" and body["correlation_id"]
    assert "nope-nope" not in response.text and "123456" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "a@example.test", "password": "x", "unexpected": "field"},
        {"email": "a@example.test", "password": "x", "code": "12"},
        {"email": "a@example.test", "password": "x", "recovery_code": "short"},
    ],
)
def test_reset_rejects_unknown_fields_and_malformed_proofs(
    recovery_client: tuple[TestClient, StubRecovery], payload: dict[str, str]
) -> None:
    client, service = recovery_client

    response = client.post("/api/v1/staff/auth/mfa/reset", json=payload, headers=HEADERS)

    assert response.status_code == 422
    assert service.reset_calls == []


def test_reset_is_refused_without_the_staff_request_boundary(
    recovery_client: tuple[TestClient, StubRecovery],
) -> None:
    client, service = recovery_client

    response = client.post(
        "/api/v1/staff/auth/mfa/reset",
        json={"email": "a@example.test", "password": "x", "code": "123456"},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 403
    assert service.reset_calls == []


def test_recovery_codes_are_returned_once_to_a_privileged_session(
    recovery_client: tuple[TestClient, StubRecovery],
) -> None:
    client, _ = recovery_client

    response = client.post(
        "/api/v1/staff/auth/mfa/recovery-codes",
        json={"password": "correct-password"},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.json() == {"recovery_codes": CODES}
    assert response.headers["cache-control"] == "no-store"


def test_recovery_codes_need_the_password_and_a_privileged_role(
    recovery_client: tuple[TestClient, StubRecovery],
) -> None:
    client, _ = recovery_client
    wrong = client.post(
        "/api/v1/staff/auth/mfa/recovery-codes", json={"password": "wrong"}, headers=HEADERS
    )
    assert wrong.status_code == 403 and wrong.json()["code"] == "password_confirmation_invalid"

    client.app.dependency_overrides[get_staff_access_user] = lambda: StaffSessionUser(  # type: ignore[attr-defined]
        id=2, email="agent@example.test", role="support_agent"
    )
    denied = client.post(
        "/api/v1/staff/auth/mfa/recovery-codes",
        json={"password": "correct-password"},
        headers=HEADERS,
    )
    assert denied.status_code == 403 and denied.json()["code"] == "staff_access_denied"

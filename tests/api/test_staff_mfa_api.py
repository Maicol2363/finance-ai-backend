"""HTTP contract of the Security Auditor password+TOTP sign-in (SEC-01 consumer)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from finance_ai.auth.dependencies import get_session_rate_limiter
from finance_ai.core.config import get_settings
from finance_ai.core.errors import MfaChallengeInvalidError, MfaCodeInvalidError
from finance_ai.main import create_app
from finance_ai.staff_auth import dependencies as staff_dependencies
from finance_ai.staff_auth.dependencies import get_staff_session_service, staff_mfa_cookie_name
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.staff_auth.service import StaffMfaChallengeIssued, StaffMfaEnrollment

ORIGIN = "https://staff.example.test"
HEADERS = {"Origin": ORIGIN, "X-Staff-Request": "1"}
CHALLENGE = "c" * 43


class StubRateLimiter:
    def check_login(self, *, client_key: str, normalized_email: str) -> None:
        del client_key, normalized_email

    def check_refresh(self, *, client_key: str) -> None:
        del client_key


class StubMfaService:
    def __init__(self) -> None:
        self.presented: list[str | None] = []
        self.cancelled: list[str | None] = []

    def login(self, command, *, correlation_id):  # type: ignore[no-untyped-def]
        del command, correlation_id
        return StaffMfaChallengeIssued(
            raw_challenge=CHALLENGE,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            enrollment_required=True,
        )

    def start_mfa_enrollment(self, raw_challenge, *, correlation_id):  # type: ignore[no-untyped-def]
        del correlation_id
        self.presented.append(raw_challenge)
        if raw_challenge != CHALLENGE:
            raise MfaChallengeInvalidError()
        return StaffMfaEnrollment(
            issuer="FinanciAI",
            account_name="auditor@example.test",
            secret="JBSWY3DPEHPK3PXP",
            otpauth_uri="otpauth://totp/FinanciAI:auditor%40example.test?secret=JBSWY3DPEHPK3PXP",
            qr_svg_data_url="data:image/svg+xml;base64,PHN2Zy8+",
        )

    def verify_mfa(self, raw_challenge, code, *, correlation_id):  # type: ignore[no-untyped-def]
        del correlation_id
        self.presented.append(raw_challenge)
        if code != "123456":
            raise MfaCodeInvalidError()
        now = datetime.now(UTC)
        return (
            StaffSessionUser(id=5, email="auditor@example.test", role="security_auditor"),
            "a" * 43,
            "b" * 43,
            now + timedelta(minutes=15),
            now + timedelta(minutes=30),
            now + timedelta(hours=8),
        )

    def cancel_mfa(self, raw_challenge):  # type: ignore[no-untyped-def]
        self.cancelled.append(raw_challenge)


@pytest.fixture
def mfa_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, StubMfaService]]:
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", f'["{ORIGIN}"]')
    get_settings.cache_clear()
    staff_dependencies._mfa_limiter.cache_clear()
    app = create_app()
    service = StubMfaService()
    app.dependency_overrides[get_staff_session_service] = lambda: service
    app.dependency_overrides[get_session_rate_limiter] = StubRateLimiter
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        yield client, service
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_auditor_password_step_returns_202_and_only_an_httponly_challenge(
    mfa_client: tuple[TestClient, StubMfaService],
) -> None:
    client, _ = mfa_client
    response = client.post(
        "/api/v1/staff/auth/login",
        json={"email": "auditor@example.test", "password": "never-return-this"},
        headers=HEADERS,
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "mfa_required" and body["enrollment_required"] is True
    assert "user" not in body
    assert CHALLENGE not in response.text and "never-return-this" not in response.text
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    assert cookies[0].startswith(f"{staff_mfa_cookie_name()}=")
    assert "HttpOnly" in cookies[0] and "Secure" in cookies[0] and "SameSite=strict" in cookies[0]
    assert response.headers["cache-control"] == "no-store"


def test_enrollment_and_verification_use_the_challenge_cookie(
    mfa_client: tuple[TestClient, StubMfaService],
) -> None:
    client, service = mfa_client
    client.cookies.set(staff_mfa_cookie_name(), CHALLENGE)
    enrollment = client.post("/api/v1/staff/auth/mfa/enrollment", json={}, headers=HEADERS)
    assert enrollment.status_code == 200
    assert enrollment.json()["qr_svg_data_url"].startswith("data:image/svg+xml;base64,")
    assert enrollment.headers["cache-control"] == "no-store"

    wrong = client.post("/api/v1/staff/auth/mfa/verify", json={"code": "000000"}, headers=HEADERS)
    assert wrong.status_code == 401 and wrong.json()["code"] == "mfa_code_invalid"
    assert "000000" not in wrong.text

    verified = client.post(
        "/api/v1/staff/auth/mfa/verify", json={"code": "123456"}, headers=HEADERS
    )
    assert verified.status_code == 200
    assert verified.json()["user"]["role"] == "security_auditor"
    cookies = verified.headers.get_list("set-cookie")
    assert len(cookies) == 3
    [cleared] = [cookie for cookie in cookies if cookie.startswith(f"{staff_mfa_cookie_name()}=")]
    assert cleared.startswith(f'{staff_mfa_cookie_name()}=""') and "Max-Age=0" in cleared
    assert "a" * 43 not in verified.text and "b" * 43 not in verified.text
    assert service.presented == [CHALLENGE, CHALLENGE, CHALLENGE]


def test_cancel_clears_the_challenge(mfa_client: tuple[TestClient, StubMfaService]) -> None:
    client, service = mfa_client
    client.cookies.set(staff_mfa_cookie_name(), CHALLENGE)
    response = client.post("/api/v1/staff/auth/mfa/cancel", json={}, headers=HEADERS)
    assert response.status_code == 204
    assert service.cancelled == [CHALLENGE]
    [cleared] = response.headers.get_list("set-cookie")
    assert cleared.startswith(f'{staff_mfa_cookie_name()}=""') and "Max-Age=0" in cleared


@pytest.mark.parametrize("payload", [{}, {"code": "12345"}, {"code": "1234567"}, {"x": "1"}])
def test_verification_body_is_strict(
    mfa_client: tuple[TestClient, StubMfaService], payload: dict[str, str]
) -> None:
    client, _ = mfa_client
    response = client.post("/api/v1/staff/auth/mfa/verify", json=payload, headers=HEADERS)
    assert response.status_code == 422


def test_mfa_routes_require_the_staff_request_boundary(
    mfa_client: tuple[TestClient, StubMfaService],
) -> None:
    client, _ = mfa_client
    response = client.post(
        "/api/v1/staff/auth/mfa/verify",
        json={"code": "123456"},
        headers={"Origin": "https://attacker.example.test", "X-Staff-Request": "1"},
    )
    assert response.status_code == 403


def test_second_factor_attempts_are_rate_limited(
    mfa_client: tuple[TestClient, StubMfaService],
) -> None:
    client, _ = mfa_client
    statuses = [
        client.post(
            "/api/v1/staff/auth/mfa/verify", json={"code": "000000"}, headers=HEADERS
        ).status_code
        for _ in range(11)
    ]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429

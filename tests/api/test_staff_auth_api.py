from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from finance_ai.auth.dependencies import get_session_rate_limiter
from finance_ai.core.config import get_settings
from finance_ai.main import create_app
from finance_ai.staff_auth.dependencies import get_staff_session_service
from finance_ai.staff_auth.schemas import StaffSessionUser

ORIGIN = "https://staff.example.test"


class StubRateLimiter:
    def check_login(self, *, client_key: str, normalized_email: str) -> None:
        del client_key, normalized_email

    def check_refresh(self, *, client_key: str) -> None:
        del client_key


class StubStaffSessionService:
    def __init__(self) -> None:
        self.cookies: tuple[str | None, str | None] | None = None
        self.fail = False

    @staticmethod
    def _user() -> StaffSessionUser:
        return StaffSessionUser(id=22, email="agent@example.test", role="support_agent")

    @staticmethod
    def _expiries() -> tuple[datetime, datetime, datetime]:
        now = datetime.now(UTC)
        return now + timedelta(minutes=15), now + timedelta(minutes=30), now + timedelta(hours=8)

    def login(self, command, *, correlation_id):  # type: ignore[no-untyped-def]
        del command, correlation_id
        access_expiry, idle_expiry, absolute_expiry = self._expiries()
        return self._user(), "a" * 43, "b" * 43, access_expiry, idle_expiry, absolute_expiry

    def refresh(self, raw_refresh, *, correlation_id):  # type: ignore[no-untyped-def]
        del correlation_id
        self.cookies = (raw_refresh, None)
        if self.fail:
            from finance_ai.core.errors import AuditUnavailableError

            raise AuditUnavailableError()
        access_expiry, idle_expiry, absolute_expiry = self._expiries()
        return self._user(), "c" * 43, "d" * 43, access_expiry, idle_expiry, absolute_expiry

    def resolve(self, raw_access, *, correlation_id):  # type: ignore[no-untyped-def]
        del correlation_id
        self.cookies = (None, raw_access)
        return self._user()

    def session_projection(self, raw_access, *, correlation_id):  # type: ignore[no-untyped-def]
        self.resolve(raw_access, correlation_id=correlation_id)
        return (self._user(), *self._expiries())

    def logout(self, raw_refresh, raw_access, *, correlation_id):  # type: ignore[no-untyped-def]
        del correlation_id
        self.cookies = (raw_refresh, raw_access)


@pytest.fixture
def staff_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, StubStaffSessionService]]:
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", '["https://staff.example.test"]')
    get_settings.cache_clear()
    app = create_app()
    service = StubStaffSessionService()
    app.dependency_overrides[get_staff_session_service] = lambda: service
    app.dependency_overrides[get_session_rate_limiter] = StubRateLimiter
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        yield client, service
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_login_issues_httponly_secure_cookies_without_credentials_in_json(
    staff_client: tuple[TestClient, StubStaffSessionService],
) -> None:
    client, _ = staff_client
    response = client.post(
        "/api/v1/staff/auth/login",
        json={"email": "agent@example.test", "password": "never-return-this"},
        headers={"Origin": ORIGIN, "X-Staff-Request": "1"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"]
    assert response.json()["user"]["role"] == "support_agent"
    assert "a" * 43 not in response.text and "b" * 43 not in response.text
    assert "never-return-this" not in response.text
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("HttpOnly" in cookie and "Secure" in cookie for cookie in cookies)
    assert all("SameSite=strict" in cookie and "Path=/" in cookie for cookie in cookies)
    assert all("Domain=" not in cookie for cookie in cookies)


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": ORIGIN},
        {"Origin": "https://attacker.example.test", "X-Staff-Request": "1"},
        {"Origin": ORIGIN, "X-Staff-Request": "1", "Sec-Fetch-Site": "cross-site"},
        {
            "Origin": ORIGIN,
            "X-Staff-Request": "1",
            "Authorization": "Bearer " + "x" * 43,
        },
    ],
)
def test_staff_boundary_rejects_missing_or_untrusted_request_proofs(
    staff_client: tuple[TestClient, StubStaffSessionService], headers: dict[str, str]
) -> None:
    client, _ = staff_client
    response = client.post(
        "/api/v1/staff/auth/logout",
        json={},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


def test_logout_passes_both_cookie_credentials_to_revocation_service(
    staff_client: tuple[TestClient, StubStaffSessionService],
) -> None:
    client, service = staff_client
    response = client.post(
        "/api/v1/staff/auth/logout",
        json={},
        headers={"Origin": ORIGIN, "X-Staff-Request": "1"},
        cookies={"finance_staff_refresh": "r" * 43, "finance_staff_access": "a" * 43},
    )
    assert response.status_code == 204
    assert response.content == b""
    assert service.cookies == ("r" * 43, "a" * 43)
    assert len(response.headers.get_list("set-cookie")) == 2


def test_credentialed_preflight_is_exact_origin_and_non_cacheable(
    staff_client: tuple[TestClient, StubStaffSessionService],
) -> None:
    client, _ = staff_client
    response = client.options(
        "/api/v1/staff/auth/login",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-staff-request",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"]


def test_refresh_audit_failure_does_not_clear_existing_cookies(
    staff_client: tuple[TestClient, StubStaffSessionService],
) -> None:
    client, service = staff_client
    service.fail = True
    response = client.post(
        "/api/v1/staff/auth/refresh",
        json={},
        headers={"Origin": ORIGIN, "X-Staff-Request": "1"},
        cookies={"finance_staff_refresh": "r" * 43, "finance_staff_access": "a" * 43},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "audit_unavailable"
    assert response.headers.get_list("set-cookie") == []

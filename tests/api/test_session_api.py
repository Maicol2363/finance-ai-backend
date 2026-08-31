from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from finance_ai.auth.dependencies import get_session_rate_limiter, get_session_service
from finance_ai.auth.session_schemas import (
    IssuedSession,
    LoginCommand,
    PublicAuthenticatedUser,
)
from finance_ai.core.errors import (
    AuthenticationRequiredError,
    InvalidCredentialsError,
    SessionInvalidError,
)
from finance_ai.main import create_app


class StubRateLimiter:
    def __init__(self) -> None:
        self.login_calls: list[tuple[str, str]] = []
        self.refresh_calls: list[str] = []

    def check_login(self, *, client_key: str, normalized_email: str) -> None:
        self.login_calls.append((client_key, normalized_email))

    def check_refresh(self, *, client_key: str) -> None:
        self.refresh_calls.append(client_key)


class StubSessionService:
    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.login_commands: list[LoginCommand] = []
        self.refresh_values: list[str] = []
        self.access_values: list[str] = []
        self.logout_values: list[str] = []

    @staticmethod
    def user() -> PublicAuthenticatedUser:
        return PublicAuthenticatedUser(
            user_id=42,
            email="person@example.com",
            preferred_currency="COP",
        )

    @classmethod
    def issued(cls) -> IssuedSession:
        now = datetime(2026, 8, 30, tzinfo=UTC)
        return IssuedSession(
            user=cls.user(),
            access_credential="a" * 43,
            renewable_credential="b" * 43,
            access_expires_at=now + timedelta(minutes=15),
            renewable_expires_at=now + timedelta(days=30),
            absolute_expires_at=now + timedelta(days=90),
        )

    def _raise_if_needed(self) -> None:
        if self.failure is not None:
            raise self.failure

    def login(self, command: LoginCommand) -> IssuedSession:
        self.login_commands.append(command)
        self._raise_if_needed()
        return self.issued()

    def refresh(self, raw_renewable: str) -> IssuedSession:
        self.refresh_values.append(raw_renewable)
        self._raise_if_needed()
        return self.issued()

    def current_user(self, raw_access: str) -> PublicAuthenticatedUser:
        self.access_values.append(raw_access)
        self._raise_if_needed()
        return self.user()

    def logout(self, raw_renewable: str) -> None:
        self.logout_values.append(raw_renewable)
        self._raise_if_needed()


@pytest.fixture
def session_client() -> Iterator[tuple[TestClient, StubSessionService, StubRateLimiter]]:
    app = create_app()
    service = StubSessionService()
    limiter = StubRateLimiter()
    app.dependency_overrides[get_session_service] = lambda: service
    app.dependency_overrides[get_session_rate_limiter] = lambda: limiter
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service, limiter


def test_login_returns_non_cacheable_opaque_session_without_password(
    session_client: tuple[TestClient, StubSessionService, StubRateLimiter],
) -> None:
    client, service, limiter = session_client
    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": " Person@Example.COM ",
            "password": "synthetic password",
            "platform": "windows",
        },
        headers={"X-Correlation-ID": "login-contract-1"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"] == "login-contract-1"
    assert response.json()["token_type"] == "Bearer"
    assert response.json()["access_credential"] == "a" * 43
    assert response.json()["renewable_credential"] == "b" * 43
    assert "password" not in response.text.casefold()
    assert service.login_commands[0].platform == "windows"
    assert limiter.login_calls[0][1] == "person@example.com"


def test_refresh_me_and_logout_follow_the_public_contract(
    session_client: tuple[TestClient, StubSessionService, StubRateLimiter],
) -> None:
    client, service, limiter = session_client
    renewable = "r" * 43
    access = "c" * 43

    refreshed = client.post(
        "/api/v1/auth/refresh",
        json={"renewable_credential": renewable},
    )
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access}"},
    )
    logout = client.post(
        "/api/v1/auth/logout",
        json={"renewable_credential": renewable},
    )

    assert refreshed.status_code == 200
    assert me.status_code == 200
    assert me.json() == {
        "id": 42,
        "email": "person@example.com",
        "preferred_currency": "COP",
    }
    assert logout.status_code == 204
    assert logout.content == b""
    assert service.refresh_values == [renewable]
    assert service.access_values == [access]
    assert service.logout_values == [renewable]
    assert len(limiter.refresh_calls) == 1


@pytest.mark.parametrize(
    ("path", "failure", "status", "code"),
    [
        ("login", InvalidCredentialsError(), 401, "invalid_credentials"),
        ("refresh", SessionInvalidError(), 401, "session_invalid"),
        ("me", AuthenticationRequiredError(), 401, "authentication_required"),
    ],
)
def test_session_failures_have_stable_safe_problem_responses(
    session_client: tuple[TestClient, StubSessionService, StubRateLimiter],
    path: str,
    failure: Exception,
    status: int,
    code: str,
) -> None:
    client, service, _ = session_client
    service.failure = failure
    secret = "s" * 43
    if path == "login":
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "private@example.com",
                "password": "private-password",
                "platform": "android",
            },
        )
    elif path == "refresh":
        response = client.post(
            "/api/v1/auth/refresh",
            json={"renewable_credential": secret},
        )
    else:
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {secret}"},
        )

    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
    assert response.headers["cache-control"] == "no-store"
    assert "private@example.com" not in response.text
    assert "private-password" not in response.text
    assert secret not in response.text
    if path != "login":
        assert response.headers["www-authenticate"] == "Bearer"


def test_non_registration_validation_uses_request_invalid(
    session_client: tuple[TestClient, StubSessionService, StubRateLimiter],
) -> None:
    client, _, _ = session_client
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "person@example.com", "password": "value", "platform": "linux"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "request_invalid"


def test_me_requires_a_well_formed_bearer_header(
    session_client: tuple[TestClient, StubSessionService, StubRateLimiter],
) -> None:
    client, service, _ = session_client
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Basic value"})
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert service.access_values == []

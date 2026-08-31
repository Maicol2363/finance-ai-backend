from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from finance_ai.auth.dependencies import (
    enforce_registration_rate_limit,
    get_registration_service,
)
from finance_ai.auth.schemas import RegistrationResult
from finance_ai.core.errors import (
    EmailAlreadyRegisteredError,
    RegistrationRateLimitedError,
    RegistrationValidationError,
    ServiceUnavailableError,
)
from finance_ai.main import create_app


class StubRegistrationService:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.commands = []

    def register(self, command: object) -> RegistrationResult:
        self.commands.append(command)
        if self.failure is not None:
            raise self.failure
        return RegistrationResult(
            user_id=42,
            normalized_email="person@example.com",
            preferred_currency="COP",
        )


@pytest.fixture
def client_and_service() -> Iterator[tuple[TestClient, StubRegistrationService]]:
    app = create_app()
    service = StubRegistrationService()
    app.dependency_overrides[get_registration_service] = lambda: service
    app.dependency_overrides[enforce_registration_rate_limit] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service


def registration_payload() -> dict[str, str]:
    return {
        "email": " Person@Example.COM ",
        "password": "a long safe password",
        "password_confirmation": "a long safe password",
    }


def set_failure(client: TestClient, service: StubRegistrationService, failure: Exception) -> None:
    service.failure = failure


def test_register_returns_public_201_without_authentication_or_secrets(
    client_and_service: tuple[TestClient, StubRegistrationService],
) -> None:
    client, service = client_and_service

    response = client.post(
        "/api/v1/auth/register",
        json=registration_payload(),
        headers={"X-Correlation-ID": "registration-test-1"},
    )

    assert response.status_code == 201
    assert response.headers["x-correlation-id"] == "registration-test-1"
    assert response.json() == {
        "user": {
            "id": 42,
            "email": "person@example.com",
            "preferred_currency": "COP",
        },
        "message": "Account created successfully. You can now log in.",
    }
    body = response.text.casefold()
    assert "password" not in body
    assert "token" not in body
    assert "session" not in body
    assert len(service.commands) == 1


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (EmailAlreadyRegisteredError(), 409, "email_already_registered"),
        (
            RegistrationValidationError({"password": ["Use a safer password."]}),
            422,
            "registration_invalid",
        ),
        (RegistrationRateLimitedError(9), 429, "registration_rate_limited"),
        (ServiceUnavailableError(), 503, "service_unavailable"),
        (RuntimeError("private database detail"), 500, "internal_error"),
    ],
)
def test_register_maps_failures_to_stable_safe_problems(
    client_and_service: tuple[TestClient, StubRegistrationService],
    failure: Exception,
    status: int,
    code: str,
) -> None:
    client, service = client_and_service
    set_failure(client, service, failure)

    response = client.post("/api/v1/auth/register", json=registration_payload())

    assert response.status_code == status
    assert response.json()["status"] == status
    assert response.json()["code"] == code
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
    assert "private database detail" not in response.text
    if status == 429:
        assert response.headers["retry-after"] == "9"


def test_pydantic_request_failure_uses_the_same_problem_contract(
    client_and_service: tuple[TestClient, StubRegistrationService],
) -> None:
    client, _ = client_and_service
    payload = registration_payload() | {"unexpected": "value"}

    response = client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "registration_invalid"
    assert "unexpected" in response.json()["field_errors"]


def test_registration_requires_json(
    client_and_service: tuple[TestClient, StubRegistrationService],
) -> None:
    client, service = client_and_service

    response = client.post(
        "/api/v1/auth/register",
        content="email=person@example.com",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"
    assert service.commands == []


def test_registration_rejects_an_oversized_declared_body(
    client_and_service: tuple[TestClient, StubRegistrationService],
) -> None:
    client, service = client_and_service

    response = client.post(
        "/api/v1/auth/register",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "20000"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert service.commands == []


def test_liveness_does_not_depend_on_the_database(
    client_and_service: tuple[TestClient, StubRegistrationService],
) -> None:
    client, _ = client_and_service

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}

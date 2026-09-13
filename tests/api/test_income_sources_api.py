from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from finance_ai.core.errors import (
    AuthenticationRequiredError,
    IncomeSourceNameTakenError,
    IncomeSourceNotFoundError,
    IncomeSourceValidationError,
)
from finance_ai.income_sources.dependencies import get_current_user_id, get_income_source_service
from finance_ai.income_sources.repositories import IncomeSourceRecord
from finance_ai.main import create_app


class StubIncomeSourceService:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.records = [IncomeSourceRecord(id=1, name="Salary")]

    def list(self, *, user_id: int) -> list[IncomeSourceRecord]:
        self._raise_if_failed()
        return list(self.records)

    def create(self, *, user_id: int, name: str) -> IncomeSourceRecord:
        self._raise_if_failed()
        record = IncomeSourceRecord(id=2, name=name.strip())
        self.records.append(record)
        return record

    def rename(self, *, user_id: int, income_source_id: int, name: str) -> IncomeSourceRecord:
        self._raise_if_failed()
        return IncomeSourceRecord(id=income_source_id, name=name.strip())

    def delete(self, *, user_id: int, income_source_id: int) -> None:
        self._raise_if_failed()

    def _raise_if_failed(self) -> None:
        if self.failure is not None:
            raise self.failure


def raise_authentication_required() -> int:
    raise AuthenticationRequiredError


@pytest.fixture
def client_and_service() -> Iterator[tuple[TestClient, StubIncomeSourceService]]:
    app = create_app()
    service = StubIncomeSourceService()
    app.dependency_overrides[get_income_source_service] = lambda: service
    app.dependency_overrides[get_current_user_id] = lambda: 42
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service


def authorization() -> dict[str, str]:
    return {"Authorization": "Bearer test-access", "X-Correlation-ID": "income-test-1"}


def test_list_requires_authentication() -> None:
    app = create_app()
    app.dependency_overrides[get_income_source_service] = lambda: StubIncomeSourceService()
    app.dependency_overrides[get_current_user_id] = raise_authentication_required
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/income-sources")
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


def test_list_returns_owned_items_only(
    client_and_service: tuple[TestClient, StubIncomeSourceService],
) -> None:
    client, _ = client_and_service
    response = client.get("/api/v1/income-sources", headers=authorization())
    assert response.status_code == 200
    assert response.json() == {"items": [{"id": 1, "name": "Salary"}]}
    assert response.headers["x-correlation-id"] == "income-test-1"


def test_create_returns_201(
    client_and_service: tuple[TestClient, StubIncomeSourceService],
) -> None:
    client, _ = client_and_service
    response = client.post(
        "/api/v1/income-sources", json={"name": " Freelance "}, headers=authorization()
    )
    assert response.status_code == 201
    assert response.json() == {"id": 2, "name": "Freelance"}


def test_create_duplicate_returns_409(
    client_and_service: tuple[TestClient, StubIncomeSourceService],
) -> None:
    client, service = client_and_service
    service.failure = IncomeSourceNameTakenError()
    response = client.post(
        "/api/v1/income-sources", json={"name": "Salary"}, headers=authorization()
    )
    assert response.status_code == 409
    assert response.json()["code"] == "income_source_name_taken"


def test_create_blank_returns_422(
    client_and_service: tuple[TestClient, StubIncomeSourceService],
) -> None:
    client, service = client_and_service
    service.failure = IncomeSourceValidationError("Enter an income source name.")
    response = client.post("/api/v1/income-sources", json={"name": " "}, headers=authorization())
    assert response.status_code == 422
    assert response.json()["code"] == "income_source_invalid"


def test_rename_unknown_returns_404(
    client_and_service: tuple[TestClient, StubIncomeSourceService],
) -> None:
    client, service = client_and_service
    service.failure = IncomeSourceNotFoundError()
    response = client.patch(
        "/api/v1/income-sources/999", json={"name": "Other"}, headers=authorization()
    )
    assert response.status_code == 404
    assert response.json()["code"] == "income_source_not_found"


def test_delete_returns_204(
    client_and_service: tuple[TestClient, StubIncomeSourceService],
) -> None:
    client, _ = client_and_service
    response = client.delete("/api/v1/income-sources/1", headers=authorization())
    assert response.status_code == 204
    assert response.content == b""


def test_errors_never_expose_internals(
    client_and_service: tuple[TestClient, StubIncomeSourceService],
) -> None:
    client, service = client_and_service
    service.failure = IncomeSourceNotFoundError()
    response = client.delete("/api/v1/income-sources/1", headers=authorization())
    assert "Traceback" not in response.text
    assert "income_source_id" not in response.text

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from finance_ai.accounts.dependencies import get_authenticated_principal
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.errors import (
    IncomeAccountUnavailableError,
    IncomeNotFoundError,
    IncomeSourceUnavailableError,
)
from finance_ai.incomes.dependencies import get_income_service
from finance_ai.incomes.schemas import IncomeRecord, IncomeWriteRequest
from finance_ai.main import create_app

NOW = datetime(2026, 9, 17, tzinfo=UTC)


def record() -> IncomeRecord:
    return IncomeRecord(
        id=7,
        account_id=3,
        account_name="Cash",
        currency_code="COP",
        amount=Decimal("25.50"),
        date=date(2026, 9, 18),
        description=None,
        income_source_id=None,
        income_source_name=None,
        created_at=NOW,
        updated_at=NOW,
    )


class StubIncomeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, object]] = []
        self.failure: Exception | None = None

    def _result(self) -> IncomeRecord:
        if self.failure:
            raise self.failure
        return record()

    def create(self, *, owner_id: int, request: IncomeWriteRequest) -> IncomeRecord:
        self.calls.append(("create", owner_id, request))
        return self._result()

    def list(self, *, owner_id: int, account_id: int | None = None) -> list[IncomeRecord]:
        self.calls.append(("list", owner_id, account_id))
        return [self._result()]

    def get(self, *, owner_id: int, income_id: int) -> IncomeRecord:
        self.calls.append(("get", owner_id, income_id))
        return self._result()

    def update(self, *, owner_id: int, income_id: int, request: IncomeWriteRequest) -> IncomeRecord:
        self.calls.append(("update", owner_id, (income_id, request)))
        return self._result()

    def delete(self, *, owner_id: int, income_id: int) -> None:
        self.calls.append(("delete", owner_id, income_id))
        self._result()


@pytest.fixture
def client_and_service() -> Iterator[tuple[TestClient, StubIncomeService]]:
    app = create_app()
    service = StubIncomeService()
    app.dependency_overrides[get_income_service] = lambda: service
    app.dependency_overrides[get_authenticated_principal] = lambda: PublicAuthenticatedUser(
        user_id=42, email="person@example.com", preferred_currency="COP"
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service


def payload() -> dict[str, object]:
    return {"account_id": 3, "amount": "25.50", "date": "2026-09-18"}


def test_income_create_list_detail_edit_delete_contract(
    client_and_service: tuple[TestClient, StubIncomeService],
) -> None:
    client, service = client_and_service
    headers = {"X-Correlation-ID": "income-hu009-1"}
    created = client.post("/api/v1/incomes", json=payload(), headers=headers)
    assert created.status_code == 201
    assert created.json()["amount"] == "25.50"
    assert created.json()["income_source_id"] is None
    assert created.json()["date"] == "2026-09-18"
    assert created.headers["x-correlation-id"] == "income-hu009-1"
    assert client.get("/api/v1/incomes?account_id=3").json()["items"][0]["id"] == 7
    assert client.get("/api/v1/incomes/7").json()["id"] == 7
    assert client.put("/api/v1/incomes/7", json=payload()).status_code == 200
    assert client.delete("/api/v1/incomes/7").status_code == 204
    assert [name for name, _, _ in service.calls] == ["create", "list", "get", "update", "delete"]
    assert all(owner_id == 42 for _, owner_id, _ in service.calls)


@pytest.mark.parametrize("amount", ["0.00", "-1.00", "1.001", 12])
def test_income_rejects_invalid_amount_before_service(
    client_and_service: tuple[TestClient, StubIncomeService], amount: object
) -> None:
    client, service = client_and_service
    response = client.post("/api/v1/incomes", json={**payload(), "amount": amount})
    assert response.status_code == 422
    assert response.json()["code"] == "request_invalid"
    assert not service.calls


@pytest.mark.parametrize(
    "failure",
    [IncomeAccountUnavailableError(), IncomeSourceUnavailableError(), IncomeNotFoundError()],
)
def test_income_failures_have_safe_stable_problem(
    client_and_service: tuple[TestClient, StubIncomeService], failure: Exception
) -> None:
    client, service = client_and_service
    service.failure = failure
    response = client.post(
        "/api/v1/incomes",
        json=payload(),
        headers={"X-Correlation-ID": "income-failure-1"},
    )
    assert response.status_code in (404, 422)
    assert response.json()["correlation_id"] == "income-failure-1"
    assert "person@example.com" not in response.text
    assert "Traceback" not in response.text


def test_income_openapi_declares_all_operations() -> None:
    contract = create_app().openapi()
    paths = contract["paths"]
    assert set(paths["/api/v1/incomes"]) == {"post", "get"}
    assert set(paths["/api/v1/incomes/{income_id}"]) == {"get", "put", "delete"}
    amount = contract["components"]["schemas"]["IncomeWriteRequest"]["properties"]["amount"]
    assert amount["type"] == "string"
    assert amount["pattern"].startswith("^(?=.*[1-9])")

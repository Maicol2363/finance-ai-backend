from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.errors import ExpenseNotFoundError
from finance_ai.expenses.dependencies import get_expense_service
from finance_ai.expenses.repositories import (
    CategoryReference,
    CurrencyReference,
    ExpenseRecord,
    HistoryMovementRecord,
    HistoryRecord,
)
from finance_ai.expenses.router import get_authenticated_principal
from finance_ai.expenses.schemas import ExpenseListResult, ExpenseUpdateCommand, HistoryResult
from finance_ai.main import create_app

NOW = datetime(2026, 9, 15, 12, 30, tzinfo=UTC)
COP = CurrencyReference(id=1, code="COP", name="Colombian Peso", decimals=2)


def expense_record() -> ExpenseRecord:
    return ExpenseRecord(
        id=7,
        transaction_id=8,
        receipt_id=9,
        name="Groceries",
        amount=Decimal("25.00"),
        quantity=Decimal("1"),
        unit_price=Decimal("25.00"),
        note=None,
        category=CategoryReference(3, "Needs"),
        receipt_name="Supermarket",
        receipt_note=None,
        store="Supermarket",
        description="Weekly groceries",
        occurred_at=NOW,
        status="confirmed",
        capture_source="manual",
        account_id=11,
        account_name="Cash",
        account_is_active=True,
        currency=COP,
        created_at=NOW,
        updated_at=NOW,
    )


class StubExpenseService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure: Exception | None = None

    def list(self, *, owner_id: int, page: int, page_size: int) -> ExpenseListResult:
        self.calls.append(("list", (owner_id, page, page_size)))
        return ExpenseListResult(items=(expense_record(),), total=1, page=page, page_size=page_size)

    def get(self, *, owner_id: int, expense_id: int) -> ExpenseRecord:
        self.calls.append(("get", (owner_id, expense_id)))
        if self.failure:
            raise self.failure
        return expense_record()

    def update(
        self, *, owner_id: int, expense_id: int, command: ExpenseUpdateCommand
    ) -> ExpenseRecord:
        self.calls.append(("update", (owner_id, expense_id, command)))
        return expense_record()

    def delete(self, *, owner_id: int, expense_id: int) -> None:
        self.calls.append(("delete", (owner_id, expense_id)))

    def history(self, **kwargs: object) -> HistoryResult:
        self.calls.append(("history", kwargs))
        return HistoryResult(
            items=(
                HistoryRecord(
                    id=8,
                    transaction_type="expense",
                    status="confirmed",
                    occurred_at=NOW,
                    description="Weekly groceries",
                    capture_source="manual",
                    income_source_name=None,
                    movements=(
                        HistoryMovementRecord(
                            account_id=11,
                            account_name="Cash",
                            amount=Decimal("-25.00"),
                            currency=COP,
                        ),
                    ),
                    expense=None,
                ),
            ),
            total=1,
            page=1,
            page_size=50,
        )


@pytest.fixture
def expense_client() -> Iterator[tuple[TestClient, StubExpenseService]]:
    app = create_app()
    service = StubExpenseService()
    principal = PublicAuthenticatedUser(
        user_id=42,
        email="person@example.com",
        preferred_currency="COP",
    )
    app.dependency_overrides[get_expense_service] = lambda: service
    app.dependency_overrides[get_authenticated_principal] = lambda: principal
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service


def test_expense_routes_use_authenticated_owner(
    expense_client: tuple[TestClient, StubExpenseService],
) -> None:
    client, service = expense_client

    listed = client.get("/api/v1/expenses", params={"page": 2, "page_size": 10})
    detail = client.get("/api/v1/expenses/7")
    updated = client.patch("/api/v1/expenses/7", json={"amount": "30.00"})
    removed = client.delete("/api/v1/expenses/7")

    assert listed.status_code == detail.status_code == updated.status_code == 200
    assert removed.status_code == 204
    assert listed.json()["items"][0]["currency"]["code"] == "COP"
    assert [item[0] for item in service.calls] == ["list", "get", "update", "delete"]
    assert service.calls[0][1] == (42, 2, 10)
    assert service.calls[1][1] == (42, 7)


def test_history_passes_combined_filters_and_preserves_currency(
    expense_client: tuple[TestClient, StubExpenseService],
) -> None:
    client, service = expense_client

    response = client.get(
        "/api/v1/history",
        params={
            "search": "  grocery ",
            "type": "expense",
            "category_id": 3,
            "account_id": 11,
            "period": "custom",
            "start_date": "2026-09-01",
            "end_date": "2026-09-15",
            "page": 2,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["movements"][0]["currency"]["code"] == "COP"
    kwargs = service.calls[0][1]
    assert kwargs["owner_id"] == 42
    assert kwargs["search"] == "  grocery "
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 10


def test_invalid_patch_and_missing_expense_use_safe_contract(
    expense_client: tuple[TestClient, StubExpenseService],
) -> None:
    client, service = expense_client

    invalid = client.patch("/api/v1/expenses/7", json={})
    service.failure = ExpenseNotFoundError()
    missing = client.get(
        "/api/v1/expenses/999",
        headers={"Authorization": "Bearer hidden", "X-Correlation-ID": "expense-404"},
    )

    assert invalid.status_code == 422
    assert invalid.json()["code"] == "request_invalid"
    assert missing.status_code == 404
    assert missing.json()["code"] == "expense_not_found"
    assert missing.json()["correlation_id"] == "expense-404"
    assert "hidden" not in missing.text


def test_expense_patch_requires_json_and_timezone_for_dates(
    expense_client: tuple[TestClient, StubExpenseService],
) -> None:
    client, _ = expense_client

    media_type = client.patch(
        "/api/v1/expenses/7",
        content='{"amount":"30.00"}',
        headers={"content-type": "text/plain"},
    )
    naive_date = client.patch(
        "/api/v1/expenses/7",
        json={"occurred_at": "2026-09-15T12:30:00"},
    )

    assert media_type.status_code == 415
    assert media_type.json()["code"] == "unsupported_media_type"
    assert naive_date.status_code == 422
    assert naive_date.json()["code"] == "request_invalid"

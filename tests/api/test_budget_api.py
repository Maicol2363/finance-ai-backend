from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from finance_ai.accounts.schemas import CurrencySummary
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.budgets.dependencies import get_authenticated_principal, get_budget_service
from finance_ai.budgets.periods import FrequencyUnit, ScheduleType
from finance_ai.budgets.schemas import (
    BudgetCategoryView,
    BudgetCreateCommand,
    BudgetListResult,
    BudgetUpdateCommand,
    BudgetView,
)
from finance_ai.core.errors import BudgetNotFoundError
from finance_ai.main import create_app

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
COP = CurrencySummary(1, "COP", "Colombian Peso", 2)
CATEGORY = BudgetCategoryView(4, "Food", "orange", "food")


def budget_view() -> BudgetView:
    return BudgetView(
        id=9,
        name="Food plan",
        category=CATEGORY,
        currency=COP,
        amount=Decimal("200.00"),
        schedule_type=ScheduleType.RECURRING,
        frequency_unit=FrequencyUnit.WEEK,
        frequency_interval=1,
        starts_on=date(2026, 9, 21),
        ends_on=None,
        current_period_start=date(2026, 9, 21),
        current_period_end=date(2026, 9, 27),
        spent_amount=Decimal("50.00"),
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


class StubBudgetService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure: Exception | None = None

    def _result(self) -> BudgetView:
        if self.failure is not None:
            raise self.failure
        return budget_view()

    def create(self, *, owner_id: int, command: BudgetCreateCommand) -> BudgetView:
        self.calls.append(("create", (owner_id, command)))
        return self._result()

    def list(self, *, owner_id: int, preferred_currency_code: str) -> BudgetListResult:
        self.calls.append(("list", (owner_id, preferred_currency_code)))
        return BudgetListResult((self._result(),), (CATEGORY,), (COP,), "COP")

    def get(self, *, owner_id: int, budget_id: int) -> BudgetView:
        self.calls.append(("get", (owner_id, budget_id)))
        return self._result()

    def update(self, *, owner_id: int, budget_id: int, command: BudgetUpdateCommand) -> BudgetView:
        self.calls.append(("update", (owner_id, budget_id, command)))
        return self._result()

    def delete(self, *, owner_id: int, budget_id: int) -> None:
        self.calls.append(("delete", (owner_id, budget_id)))
        self._result()


@pytest.fixture
def budget_client() -> Iterator[tuple[TestClient, StubBudgetService]]:
    app = create_app()
    service = StubBudgetService()
    principal = PublicAuthenticatedUser(42, "person@example.com", "COP")
    app.dependency_overrides[get_budget_service] = lambda: service
    app.dependency_overrides[get_authenticated_principal] = lambda: principal
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service


def test_budget_crud_uses_owner_and_returns_derived_progress(
    budget_client: tuple[TestClient, StubBudgetService],
) -> None:
    client, service = budget_client
    created = client.post(
        "/api/v1/budgets",
        json={
            "category_id": 4,
            "amount": "200.00",
            "currency_code": "COP",
            "period": "weekly",
        },
    )
    listed = client.get("/api/v1/budgets")
    detail = client.get("/api/v1/budgets/9")
    updated = client.patch("/api/v1/budgets/9", json={"period": "monthly"})
    removed = client.delete("/api/v1/budgets/9")

    assert created.status_code == 201
    assert created.json()["spent_amount"] == "50.00"
    assert created.json()["remaining_amount"] == "150.00"
    assert created.json()["progress_percent"] == "25.00"
    assert listed.json()["suggested_currency_code"] == "COP"
    assert detail.status_code == updated.status_code == 200
    assert removed.status_code == 204
    assert [item[0] for item in service.calls] == [
        "create",
        "list",
        "get",
        "update",
        "delete",
    ]
    assert all(item[1][0] == 42 for item in service.calls)  # type: ignore[index]


@pytest.mark.parametrize(
    "payload",
    [
        {"category_id": 4, "amount": 20, "currency_code": "COP", "period": "weekly"},
        {"category_id": 4, "amount": "0.00", "currency_code": "COP", "period": "weekly"},
        {"category_id": 4, "amount": "20.00", "currency_code": "COP", "period": "daily"},
        {
            "category_id": 4,
            "amount": "20.00",
            "currency_code": "COP",
            "period": "weekly",
            "user_id": 1,
        },
    ],
)
def test_create_rejects_invalid_or_unowned_fields_without_service_call(
    budget_client: tuple[TestClient, StubBudgetService], payload: dict[str, object]
) -> None:
    client, service = budget_client

    response = client.post("/api/v1/budgets", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "request_invalid"
    assert service.calls == []


def test_empty_or_null_patch_is_rejected(
    budget_client: tuple[TestClient, StubBudgetService],
) -> None:
    client, service = budget_client

    assert client.patch("/api/v1/budgets/9", json={}).status_code == 422
    assert client.patch("/api/v1/budgets/9", json={"amount": None}).status_code == 422
    assert service.calls == []


def test_advanced_schedule_and_all_expenses_scope_reach_service(
    budget_client: tuple[TestClient, StubBudgetService],
) -> None:
    client, service = budget_client

    response = client.post(
        "/api/v1/budgets",
        json={
            "category_id": None,
            "name": "Trip",
            "amount": "900.00",
            "currency_code": "USD",
            "schedule": {
                "schedule_type": "one_time",
                "frequency_unit": None,
                "frequency_interval": None,
                "starts_on": "2026-12-01",
                "ends_on": "2026-12-20",
            },
        },
    )

    assert response.status_code == 201
    _, (_, command) = service.calls[0]
    assert isinstance(command, BudgetCreateCommand)
    assert command.category_id is None
    assert command.name == "Trip"
    assert command.schedule is not None
    assert command.schedule.schedule_type is ScheduleType.ONE_TIME
    assert command.schedule.ends_on == date(2026, 12, 20)


@pytest.mark.parametrize(
    "schedule",
    [
        {
            "schedule_type": "recurring",
            "frequency_unit": "week",
            "starts_on": "2026-09-01",
        },
        {
            "schedule_type": "one_time",
            "frequency_unit": "week",
            "frequency_interval": 1,
            "starts_on": "2026-09-01",
        },
        {
            "schedule_type": "one_time",
            "starts_on": "2026-09-20",
            "ends_on": "2026-09-01",
        },
    ],
)
def test_invalid_advanced_schedule_is_rejected(
    budget_client: tuple[TestClient, StubBudgetService],
    schedule: dict[str, object],
) -> None:
    client, service = budget_client

    response = client.post(
        "/api/v1/budgets",
        json={
            "amount": "20.00",
            "currency_code": "COP",
            "schedule": schedule,
        },
    )

    assert response.status_code == 422
    assert service.calls == []


def test_missing_budget_failure_is_safe(
    budget_client: tuple[TestClient, StubBudgetService],
) -> None:
    client, service = budget_client
    service.failure = BudgetNotFoundError()

    response = client.get(
        "/api/v1/budgets/999",
        headers={"X-Correlation-ID": "budget-missing"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "budget_not_found"
    assert response.json()["correlation_id"] == "budget-missing"


def test_budget_routes_require_authentication() -> None:
    app = create_app()
    service = StubBudgetService()
    app.dependency_overrides[get_budget_service] = lambda: service

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/budgets")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert service.calls == []

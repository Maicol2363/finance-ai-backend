from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from finance_ai.accounts.dependencies import (
    get_account_service,
    get_authenticated_principal,
)
from finance_ai.accounts.schemas import (
    AccountCreateCommand,
    AccountListResult,
    AccountType,
    AccountUpdateCommand,
    AccountView,
    CurrencySummary,
)
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.errors import AccountDeleteBlockedError, AccountNotFoundError
from finance_ai.main import create_app

NOW = datetime(2026, 9, 7, 19, 30, tzinfo=UTC)
CURRENCY = CurrencySummary(id=1, code="COP", name="Colombian Peso", decimals=2)


def account_view(*, is_active: bool = True) -> AccountView:
    return AccountView(
        id=11,
        name="Daily cash",
        account_type=AccountType.CASH,
        currency=CURRENCY,
        opening_balance=Decimal("100000.00"),
        balance=Decimal("85000.50"),
        currency_is_locked=True,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


class StubAccountService:
    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.calls: list[tuple[str, object]] = []

    def _result(self, *, is_active: bool = True) -> AccountView:
        if self.failure is not None:
            raise self.failure
        return account_view(is_active=is_active)

    def create(self, *, owner_id: int, command: AccountCreateCommand) -> AccountView:
        self.calls.append(("create", (owner_id, command)))
        return self._result()

    def list(
        self, *, owner_id: int, preferred_currency_code: str, include_inactive: bool
    ) -> AccountListResult:
        self.calls.append(("list", (owner_id, preferred_currency_code, include_inactive)))
        result = self._result()
        return AccountListResult(
            accounts=(result,),
            suggested_currency_code="COP",
            supported_currencies=(CURRENCY,),
            account_types=tuple(AccountType),
        )

    def get(self, *, owner_id: int, account_id: int) -> AccountView:
        self.calls.append(("get", (owner_id, account_id)))
        return self._result()

    def update(
        self, *, owner_id: int, account_id: int, command: AccountUpdateCommand
    ) -> AccountView:
        self.calls.append(("update", (owner_id, account_id, command)))
        return self._result()

    def set_active(self, *, owner_id: int, account_id: int, is_active: bool) -> AccountView:
        self.calls.append(("status", (owner_id, account_id, is_active)))
        return self._result(is_active=is_active)

    def delete(self, *, owner_id: int, account_id: int) -> None:
        self.calls.append(("delete", (owner_id, account_id)))
        self._result()


@pytest.fixture
def account_client() -> Iterator[tuple[TestClient, StubAccountService]]:
    app = create_app()
    service = StubAccountService()
    principal = PublicAuthenticatedUser(
        user_id=42,
        email="person@example.com",
        preferred_currency="COP",
    )
    app.dependency_overrides[get_account_service] = lambda: service
    app.dependency_overrides[get_authenticated_principal] = lambda: principal
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service


def test_create_returns_exact_non_cacheable_contract_without_owner_field(
    account_client: tuple[TestClient, StubAccountService],
) -> None:
    client, service = account_client
    response = client.post(
        "/api/v1/accounts",
        json={
            "name": "Daily cash",
            "type": "cash",
            "currency_code": "COP",
            "opening_balance": "100000.00",
        },
        headers={"X-Correlation-ID": "account-create-1"},
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"] == "account-create-1"
    assert response.json()["opening_balance"] == "100000.00"
    assert response.json()["balance"] == "85000.50"
    assert response.json()["currency_is_locked"] is True
    assert "user_id" not in response.json()
    operation, (owner_id, command) = service.calls[0]  # type: ignore[misc]
    assert operation == "create"
    assert owner_id == 42
    assert command.opening_balance == Decimal("100000.00")


def test_list_detail_update_and_status_use_the_authenticated_owner(
    account_client: tuple[TestClient, StubAccountService],
) -> None:
    client, service = account_client

    listed = client.get("/api/v1/accounts?include_inactive=true")
    detail = client.get("/api/v1/accounts/11")
    updated = client.patch("/api/v1/accounts/11", json={"name": "Pocket"})
    inactive = client.patch("/api/v1/accounts/11/status", json={"is_active": False})

    assert listed.status_code == detail.status_code == updated.status_code == 200
    assert inactive.status_code == 200
    assert listed.json()["suggested_currency_code"] == "COP"
    assert len(listed.json()["account_types"]) == 6
    assert inactive.json()["is_active"] is False
    assert [call[0] for call in service.calls] == ["list", "get", "update", "status"]
    assert all(call[1][0] == 42 for call in service.calls)  # type: ignore[index]


def test_account_routes_require_bearer_authentication() -> None:
    app = create_app()
    service = StubAccountService()
    app.dependency_overrides[get_account_service] = lambda: service
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/accounts")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
    assert response.headers["cache-control"] == "no-store"
    assert service.calls == []


def test_missing_and_foreign_account_failure_is_stable_and_safe(
    account_client: tuple[TestClient, StubAccountService],
) -> None:
    client, service = account_client
    service.failure = AccountNotFoundError()
    secret_name = "Private platinum account"

    response = client.patch(
        "/api/v1/accounts/987",
        json={"name": secret_name},
        headers={"Authorization": "Bearer hidden-access", "X-Correlation-ID": "safe-404"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "account_not_found"
    assert response.json()["correlation_id"] == "safe-404"
    assert response.headers["x-correlation-id"] == "safe-404"
    assert secret_name not in response.text
    assert "hidden-access" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": None},
        {"opening_balance": 12.34},
        {"opening_balance": 12},
        {"name": "Cash", "user_id": 7},
    ],
)
def test_invalid_patches_reject_values_without_calling_service(
    account_client: tuple[TestClient, StubAccountService], payload: dict[str, object]
) -> None:
    client, service = account_client
    response = client.patch("/api/v1/accounts/11", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "request_invalid"
    assert service.calls == []
    assert "12.34" not in response.text


def test_content_type_body_bound_and_delete_contract(
    account_client: tuple[TestClient, StubAccountService],
) -> None:
    client, service = account_client
    wrong_type = client.post(
        "/api/v1/accounts",
        content="name=Cash",
        headers={"Content-Type": "text/plain"},
    )
    removed = client.delete(
        "/api/v1/accounts/11",
        headers={"X-Correlation-ID": "account-delete-1"},
    )

    assert wrong_type.status_code == 415
    assert wrong_type.json()["code"] == "unsupported_media_type"
    assert removed.status_code == 204
    assert removed.content == b""
    assert removed.headers["cache-control"] == "no-store"
    assert removed.headers["x-correlation-id"] == "account-delete-1"
    assert ("delete", (42, 11)) in service.calls
    assert "delete" in create_app().openapi()["paths"]["/api/v1/accounts/{account_id}"]


def test_openapi_matches_exact_money_patch_and_bearer_contract() -> None:
    contract = create_app().openapi()
    schemas = contract["components"]["schemas"]
    create_balance = schemas["AccountCreateRequest"]["properties"]["opening_balance"]
    update = schemas["AccountUpdateRequest"]["properties"]

    assert create_balance["type"] == "string"
    assert update["opening_balance"]["type"] == "string"
    assert update["name"]["type"] == "string"
    assert "anyOf" not in update["name"]
    assert contract["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "Access credential returned by login or session refresh.",
    }
    assert contract["paths"]["/api/v1/accounts"]["get"]["security"] == [{"BearerAuth": []}]


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/api/v1/accounts/11/extra", 404, "route_not_found"),
        ("POST", "/api/v1/accounts/11", 405, "method_not_allowed"),
    ],
)
def test_unmatched_account_routes_use_safe_diagnostic_contract(
    method: str, path: str, status: int, code: str
) -> None:
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.request(
            method,
            path,
            json={} if method == "POST" else None,
            headers={"X-Correlation-ID": "account-route-error"},
        )

    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["correlation_id"] == "account-route-error"
    assert response.headers["x-correlation-id"] == "account-route-error"
    assert response.headers["cache-control"] == "no-store"


def test_delete_blocked_failure_is_stable_and_contains_no_financial_data(
    account_client: tuple[TestClient, StubAccountService],
) -> None:
    client, service = account_client
    service.failure = AccountDeleteBlockedError()

    response = client.delete(
        "/api/v1/accounts/11",
        headers={"X-Correlation-ID": "blocked-delete"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "account_delete_blocked"
    assert response.json()["correlation_id"] == "blocked-delete"
    assert "Daily cash" not in response.text
    assert "85000.50" not in response.text

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.categories.dependencies import get_authenticated_principal, get_category_service
from finance_ai.categories.schemas import (
    CategoryColor,
    CategoryCreateCommand,
    CategoryIcon,
    CategoryListResult,
    CategoryUpdateCommand,
    CategoryView,
)
from finance_ai.core.errors import CategoryNotFoundError
from finance_ai.main import create_app

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def category_view() -> CategoryView:
    return CategoryView(
        id=4,
        name="Food",
        color=CategoryColor.ORANGE,
        icon=CategoryIcon.FOOD,
        sort_order=0,
        created_at=NOW,
        updated_at=NOW,
    )


class StubCategoryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure: Exception | None = None

    def _view(self) -> CategoryView:
        if self.failure:
            raise self.failure
        return category_view()

    def create(self, *, owner_id: int, command: CategoryCreateCommand) -> CategoryView:
        self.calls.append(("create", (owner_id, command)))
        return self._view()

    def list(self, *, owner_id: int) -> CategoryListResult:
        self.calls.append(("list", owner_id))
        return CategoryListResult((self._view(),), tuple(CategoryColor), tuple(CategoryIcon))

    def get(self, *, owner_id: int, category_id: int) -> CategoryView:
        self.calls.append(("get", (owner_id, category_id)))
        return self._view()

    def update(
        self, *, owner_id: int, category_id: int, command: CategoryUpdateCommand
    ) -> CategoryView:
        self.calls.append(("update", (owner_id, category_id, command)))
        return self._view()

    def reorder(self, *, owner_id: int, category_ids: list[int]) -> CategoryListResult:
        self.calls.append(("reorder", (owner_id, category_ids)))
        return CategoryListResult((self._view(),), tuple(CategoryColor), tuple(CategoryIcon))

    def delete(self, *, owner_id: int, category_id: int) -> None:
        self.calls.append(("delete", (owner_id, category_id)))
        self._view()


@pytest.fixture
def category_client() -> Iterator[tuple[TestClient, StubCategoryService]]:
    app = create_app()
    service = StubCategoryService()
    principal = PublicAuthenticatedUser(
        user_id=42, email="person@example.com", preferred_currency="COP"
    )
    app.dependency_overrides[get_category_service] = lambda: service
    app.dependency_overrides[get_authenticated_principal] = lambda: principal
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service


def test_category_routes_cover_crud_order_and_owner(
    category_client: tuple[TestClient, StubCategoryService],
) -> None:
    client, service = category_client
    created = client.post(
        "/api/v1/categories", json={"name": "Food", "color": "orange", "icon": "food"}
    )
    listed = client.get("/api/v1/categories")
    detail = client.get("/api/v1/categories/4")
    updated = client.patch("/api/v1/categories/4", json={"name": "Dining"})
    ordered = client.put("/api/v1/categories/order", json={"category_ids": [4]})
    removed = client.delete("/api/v1/categories/4")

    assert [
        created.status_code,
        listed.status_code,
        detail.status_code,
        updated.status_code,
        ordered.status_code,
        removed.status_code,
    ] == [201, 200, 200, 200, 200, 204]
    assert created.json()["icon"] == "food"
    assert len(listed.json()["supported_colors"]) == 9
    assert len(ordered.json()["supported_icons"]) == 13
    assert [call[0] for call in service.calls] == [
        "create",
        "list",
        "get",
        "update",
        "reorder",
        "delete",
    ]
    assert all((call[1] == 42 or call[1][0] == 42) for call in service.calls)  # type: ignore[index]


def test_category_auth_validation_and_safe_foreign_failure(
    category_client: tuple[TestClient, StubCategoryService],
) -> None:
    client, service = category_client
    invalid = client.patch("/api/v1/categories/4", json={})
    assert invalid.status_code == 422

    service.failure = CategoryNotFoundError()
    missing = client.get("/api/v1/categories/999", headers={"X-Correlation-ID": "category-404"})
    assert missing.status_code == 404
    assert missing.json()["code"] == "category_not_found"
    assert missing.json()["correlation_id"] == "category-404"
    assert "Food" not in missing.text

    app = create_app()
    app.dependency_overrides[get_category_service] = lambda: service
    with TestClient(app, raise_server_exceptions=False) as anonymous:
        denied = anonymous.get("/api/v1/categories")
    assert denied.status_code == 401
    assert denied.json()["code"] == "authentication_required"


def test_category_openapi_has_owned_contract() -> None:
    contract = create_app().openapi()
    paths = contract["paths"]
    assert set(paths["/api/v1/categories"]) >= {"get", "post"}
    assert "put" in paths["/api/v1/categories/order"]
    assert set(paths["/api/v1/categories/{category_id}"]) >= {"get", "patch", "delete"}
    assert paths["/api/v1/categories"]["get"]["security"] == [{"BearerAuth": []}]

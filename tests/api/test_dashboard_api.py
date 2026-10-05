from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.dashboard.dependencies import (
    get_authenticated_principal,
    get_dashboard_service,
)
from finance_ai.dashboard.service import DashboardService
from finance_ai.main import create_app
from tests.unit.test_dashboard_service import StubDashboardRepository


@pytest.fixture
def dashboard_client() -> Iterator[tuple[TestClient, StubDashboardRepository]]:
    app = create_app()
    repo = StubDashboardRepository()
    service = DashboardService(repo)
    principal = PublicAuthenticatedUser(13, "contractor@example.com", "COP")

    app.dependency_overrides[get_dashboard_service] = lambda: service
    app.dependency_overrides[get_authenticated_principal] = lambda: principal

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, repo


def test_dashboard_summary_success(
    dashboard_client: tuple[TestClient, StubDashboardRepository],
) -> None:
    client, _ = dashboard_client
    response = client.get(
        "/api/v1/dashboard/summary",
        params={
            "start_date": "2026-10-01",
            "end_date": "2026-10-31",
            "currency": "COP",
        },
    )

    assert response.status_code == 200
    data = response.json()

    assert data["period"]["start"] == "2026-10-01"
    assert data["period"]["end"] == "2026-10-31"
    assert data["flow"]["incomes"] == "5000000.00"
    assert data["flow"]["expenses"] == "2000000.00"
    assert data["flow"]["net"] == "3000000.00"
    assert "weekly" in data
    assert len(data["weekly"]) == 2
    assert "top_categories" in data
    assert "projection" in data
    assert "balance" in data
    assert data["balance"]["runway_months"] == "4.0"
    assert "income_average" in data
    assert "weekday_pattern" in data
    assert "recurring_candidates" in data
    assert "recent" in data


def test_dashboard_summary_invalid_date_range(
    dashboard_client: tuple[TestClient, StubDashboardRepository],
) -> None:
    client, _ = dashboard_client
    response = client.get(
        "/api/v1/dashboard/summary",
        params={
            "start_date": "2026-10-31",
            "end_date": "2026-10-01",
            "currency": "COP",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "dashboard_date_range_invalid"


def test_dashboard_summary_invalid_currency(
    dashboard_client: tuple[TestClient, StubDashboardRepository],
) -> None:
    client, _ = dashboard_client
    response = client.get(
        "/api/v1/dashboard/summary",
        params={
            "start_date": "2026-10-01",
            "end_date": "2026-10-31",
            "currency": "INVALID",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "dashboard_currency_invalid"


def test_dashboard_summary_unauthorized() -> None:
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/dashboard/summary",
            params={
                "start_date": "2026-10-01",
                "end_date": "2026-10-31",
                "currency": "COP",
            },
        )
        assert response.status_code == 401

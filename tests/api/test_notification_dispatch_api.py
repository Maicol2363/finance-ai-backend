"""The scheduled dispatch endpoint: secret header, rate limit, hidden from the public contract."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from finance_ai.core.config import Settings, get_settings
from finance_ai.main import create_app
from finance_ai.notifications import router as dispatch_router
from finance_ai.notifications.dispatcher import DispatchSummary

TOKEN = "d" * 40
URL = "/internal/notifications/dispatch"


@dataclass
class FakeDispatcher:
    calls: int = 0

    def dispatch_once(self) -> DispatchSummary:
        self.calls += 1
        return DispatchSummary(sent=2, skipped=1, retried=0, failed=0)


def build_client(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, fake: FakeDispatcher | None = None
) -> TestClient:
    dispatch_router._limiter.cache_clear()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    if fake is not None:
        monkeypatch.setattr(dispatch_router, "get_dispatcher", lambda: fake)
    return TestClient(app)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeDispatcher]]:
    fake = FakeDispatcher()
    settings = Settings(_env_file=None, notification_dispatch_token=TOKEN)
    yield build_client(monkeypatch, settings, fake), fake
    dispatch_router._limiter.cache_clear()


def test_the_endpoint_is_disabled_until_a_token_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDispatcher()
    http = build_client(monkeypatch, Settings(_env_file=None), fake)

    response = http.post(URL, headers={"X-Dispatch-Token": TOKEN})

    assert (
        response.status_code == 503 and response.json()["code"] == "notification_dispatch_disabled"
    )
    assert fake.calls == 0


@pytest.mark.parametrize(
    "headers", [{}, {"X-Dispatch-Token": "wrong"}, {"X-Dispatch-Token": TOKEN + "x"}]
)
def test_a_missing_or_wrong_token_is_refused_without_running_anything(
    client: tuple[TestClient, FakeDispatcher], headers: dict[str, str]
) -> None:
    http, fake = client

    response = http.post(URL, headers=headers)

    assert response.status_code == 401 and response.json()["code"] == "dispatch_token_invalid"
    assert TOKEN not in response.text and fake.calls == 0


def test_the_right_token_runs_one_sweep_and_reports_counts(
    client: tuple[TestClient, FakeDispatcher],
) -> None:
    http, fake = client

    response = http.post(URL, headers={"X-Dispatch-Token": TOKEN})

    assert response.status_code == 200
    assert response.json() == {"sent": 2, "skipped": 1, "retried": 0, "failed": 0}
    assert fake.calls == 1


def test_strangers_without_the_token_cannot_use_up_the_schedulers_budget(
    client: tuple[TestClient, FakeDispatcher],
) -> None:
    http, fake = client

    refused = [http.post(URL, headers={"X-Dispatch-Token": "wrong"}).status_code for _ in range(40)]
    real = http.post(URL, headers={"X-Dispatch-Token": TOKEN})

    assert set(refused) == {401} and real.status_code == 200 and fake.calls == 1


def test_the_endpoint_is_rate_limited(client: tuple[TestClient, FakeDispatcher]) -> None:
    http, _ = client

    statuses = [http.post(URL, headers={"X-Dispatch-Token": TOKEN}).status_code for _ in range(31)]

    assert statuses[:30] == [200] * 30 and statuses[30] == 429


def test_the_route_is_not_part_of_the_public_api_contract(
    client: tuple[TestClient, FakeDispatcher],
) -> None:
    http, _ = client

    paths = http.get("/openapi.json").json()["paths"]

    assert not any("internal" in path or "dispatch" in path for path in paths)


def test_production_without_a_real_sender_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+psycopg://u:p@db.example.com/finance_ai",
        session_limiter_secret="a-production-limiter-secret-value-1",
        notification_dispatch_token=TOKEN,
    )
    http = build_client(monkeypatch, settings)
    monkeypatch.setattr(dispatch_router, "get_settings", lambda: settings)

    response = http.post(URL, headers={"X-Dispatch-Token": TOKEN})

    assert response.status_code == 503 and response.json()["code"] == "notifications_unavailable"

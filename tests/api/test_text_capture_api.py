"""HU-013 HTTP contract: auth, validation, stable errors, rate limit and privacy."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.accounts.dependencies import get_authenticated_principal
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.capture import dependencies as capture_dependencies
from finance_ai.capture.dependencies import get_text_capture_service
from finance_ai.capture.gateway import TextProviderError
from finance_ai.main import create_app
from finance_ai.receipts.guards import ReceiptRateLimiter
from tests.capture_support import (
    CLIENT_NOW,
    FakeGateway,
    build_service,
    raw,
)

URL = "/api/v1/capture/text/proposals"
TEXT = "Almorcé en Subway 15900 con efectivo"
SECRET_TEXT = "ALMUERZO-PRIVADO-777"


def body(text: str = TEXT, **extra: Any) -> dict[str, Any]:
    return {"text": text, "client_now": CLIENT_NOW.isoformat(), **extra}


def expense_answer() -> object:
    return raw(
        intent="expense",
        amount=15900,
        description="Almuerzo",
        account="Efectivo",
        category="Comida",
    )


def make_client(
    session_factory: sessionmaker[Session], gateway: FakeGateway, *, user_id: int = 42
) -> Iterator[TestClient]:
    app = create_app()
    service, *_ = build_service(session_factory, gateway)
    app.dependency_overrides[get_text_capture_service] = lambda: service
    app.dependency_overrides[get_authenticated_principal] = lambda: PublicAuthenticatedUser(
        user_id=user_id, email="person@example.com", preferred_currency="COP"
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture(autouse=True)
def fresh_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        capture_dependencies,
        "_rate_limiter",
        ReceiptRateLimiter(capacity=3.0, requests_per_minute=6.0),
    )


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    yield from make_client(session_factory, FakeGateway(*[expense_answer()] * 10))


def test_proposal_contract_and_no_store_headers(client: TestClient) -> None:
    response = client.post(URL, json=body(), headers={"X-Correlation-ID": "cap-1"})

    assert response.status_code == 201
    assert response.headers["X-Correlation-ID"] == "cap-1"
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    assert data["status"] == "ready"
    assert data["intent"] == "expense"
    assert data["one_tap"] is True
    assert data["amount"] == "15900.00"
    assert data["account"] == {"id": 1, "name": "Efectivo", "currency_code": "COP"}
    assert data["original_text"] == TEXT


def test_the_draft_can_be_read_back_and_discarded(client: TestClient) -> None:
    proposal_id = client.post(URL, json=body()).json()["proposal_id"]

    assert client.get(f"{URL}/{proposal_id}").json()["revision"] == 1
    assert client.delete(f"{URL}/{proposal_id}").status_code == 204
    gone = client.post(f"{URL}/{proposal_id}/confirm", json=_confirm()).json()
    assert gone["code"] == "text_proposal_closed"


def _confirm(**overrides: Any) -> dict[str, Any]:
    return {
        "intent": "expense",
        "amount": "15900.00",
        "date": CLIENT_NOW.date().isoformat(),
        "account_id": 1,
        "category_id": 10,
        **overrides,
    }


def test_confirm_returns_201_then_200_for_a_repeat(client: TestClient) -> None:
    proposal_id = client.post(URL, json=body()).json()["proposal_id"]

    first = client.post(f"{URL}/{proposal_id}/confirm", json=_confirm())
    again = client.post(f"{URL}/{proposal_id}/confirm", json=_confirm())

    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["record_id"] == again.json()["record_id"]
    assert again.json()["replayed"] is True


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        (body(text="   "), "__root__"),
        (body(text="x" * 1001), "text"),
        ({"text": "hola"}, "client_now"),
        ({"text": "hola", "client_now": "2026-10-02T10:00:00"}, "client_now"),
        (body(extra_field=1), "extra_field"),
        (
            {"client_now": CLIENT_NOW.isoformat(), "choice": {"field": "account", "value": "1"}},
            None,
        ),
    ],
)
def test_invalid_requests_use_the_shared_problem_shape(
    client: TestClient, payload: dict[str, Any], field: str | None
) -> None:
    response = client.post(URL, json=payload, headers={"X-Correlation-ID": "cap-422"})

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "request_invalid"
    assert problem["correlation_id"] == "cap-422"


def test_non_json_bodies_are_rejected(client: TestClient) -> None:
    response = client.post(URL, content="texto", headers={"Content-Type": "text/plain"})

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_oversized_bodies_are_rejected(client: TestClient) -> None:
    response = client.post(URL, json=body(text="a" * 20_000))

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"


def test_a_provider_outage_is_a_503_with_a_safe_message_and_no_echo(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = FakeGateway(TextProviderError("rejected", status_code=401))
    for client in make_client(session_factory, gateway):
        response = client.post(URL, json=body(SECRET_TEXT), headers={"X-Correlation-ID": "cap-503"})

    assert response.status_code == 503
    problem = response.json()
    assert problem["code"] == "text_capture_unavailable"
    assert problem["correlation_id"] == "cap-503"
    rendered = json.dumps(problem) + json.dumps(dict(response.headers))
    assert SECRET_TEXT not in rendered
    assert "401" not in problem["detail"]


def test_a_burst_of_messages_is_limited_with_retry_after(client: TestClient) -> None:
    codes = [client.post(URL, json=body()).status_code for _ in range(5)]
    limited = client.post(URL, json=body())

    assert codes[:3] == [201, 201, 201]
    assert limited.status_code == 429
    assert limited.json()["code"] == "text_capture_rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1


def test_foreign_drafts_look_exactly_like_missing_ones(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = FakeGateway(expense_answer())
    for owner in make_client(session_factory, gateway, user_id=1):
        proposal_id = owner.post(URL, json=body()).json()["proposal_id"]
    for stranger in make_client(session_factory, gateway, user_id=2):
        foreign = stranger.get(f"{URL}/{proposal_id}")
        missing = stranger.get(f"{URL}/00000000-0000-4000-8000-000000000000")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"] == "text_proposal_not_found"


def test_authentication_is_required() -> None:
    with TestClient(create_app(), raise_server_exceptions=False) as anonymous:
        response = anonymous.post(URL, json=body())

    assert response.status_code == 401


def test_a_server_without_a_key_says_so_and_stays_safe(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = FakeGateway(TextProviderError("not_configured"))
    for client in make_client(session_factory, gateway):
        response = client.post(URL, json=body(SECRET_TEXT), headers={"X-Correlation-ID": "cap-cfg"})

    assert response.status_code == 503
    problem = response.json()
    assert problem["code"] == "text_capture_not_configured"
    assert problem["correlation_id"] == "cap-cfg"
    assert SECRET_TEXT not in json.dumps(problem)


def test_tapped_options_do_not_spend_the_message_budget(
    session_factory: sessionmaker[Session],
) -> None:
    question = raw(intent="expense", amount=13000)
    gateway = FakeGateway(question, *[expense_answer()] * 3)
    for client in make_client(session_factory, gateway):
        first = client.post(URL, json=body("13 mil pesos"))
        proposal_id = first.json()["proposal_id"]
        taps = [
            client.post(
                URL,
                json={
                    "client_now": CLIENT_NOW.isoformat(),
                    "proposal_id": proposal_id,
                    "choice": {"field": "account", "value": "1"},
                },
            ).status_code
            for _ in range(8)
        ]
        typed = client.post(URL, json=body("otro mensaje"))

    assert first.status_code == 201
    assert taps == [201] * 8
    assert typed.status_code == 201

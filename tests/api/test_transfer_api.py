from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from finance_ai.accounts.dependencies import get_authenticated_principal
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.errors import (
    TransferAccountUnavailableError,
    TransferDestinationAmountRequiredError,
    TransferSameAccountError,
    TransferSourceDestinationMismatchError,
)
from finance_ai.main import create_app
from finance_ai.transfers.dependencies import get_transfer_service
from finance_ai.transfers.schemas import (
    TransferAccountSummary,
    TransferCreateRequest,
    TransferRecord,
)

NOW = datetime(2026, 9, 18, tzinfo=UTC)


def record() -> TransferRecord:
    return TransferRecord(
        id=9,
        source_account=TransferAccountSummary(id=3, name="Cash", currency_code="COP"),
        source_amount=Decimal("100.00"),
        destination_account=TransferAccountSummary(id=4, name="Savings", currency_code="COP"),
        destination_amount=Decimal("100.00"),
        date=date(2026, 9, 18),
        description=None,
        created_at=NOW,
        updated_at=NOW,
    )


class StubTransferService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, object]] = []
        self.failure: Exception | None = None

    def create(self, *, owner_id: int, request: TransferCreateRequest) -> TransferRecord:
        self.calls.append(("create", owner_id, request))
        if self.failure:
            raise self.failure
        return record()


@pytest.fixture
def client_and_service() -> Iterator[tuple[TestClient, StubTransferService]]:
    app = create_app()
    service = StubTransferService()
    app.dependency_overrides[get_transfer_service] = lambda: service
    app.dependency_overrides[get_authenticated_principal] = lambda: PublicAuthenticatedUser(
        user_id=42, email="person@example.com", preferred_currency="COP"
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, service


def payload() -> dict[str, object]:
    return {
        "source_account_id": 3,
        "destination_account_id": 4,
        "source_amount": "100.00",
        "date": "2026-09-18",
    }


def test_transfer_create_contract(
    client_and_service: tuple[TestClient, StubTransferService],
) -> None:
    client, service = client_and_service
    created = client.post(
        "/api/v1/transfers", json=payload(), headers={"X-Correlation-ID": "transfer-1"}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["source_amount"] == "100.00"
    assert body["destination_amount"] == "100.00"
    assert body["source_account"]["id"] == 3
    assert body["destination_account"]["id"] == 4
    assert created.headers["x-correlation-id"] == "transfer-1"
    assert service.calls[0][1] == 42


def test_transfer_failures_use_safe_contract(
    client_and_service: tuple[TestClient, StubTransferService],
) -> None:
    client, service = client_and_service
    cases = [
        (TransferSameAccountError(), 422, "transfer_same_account"),
        (TransferAccountUnavailableError("source_account_id"), 422, "transfer_account_unavailable"),
        (
            TransferDestinationAmountRequiredError(),
            422,
            "transfer_destination_amount_required",
        ),
        (
            TransferSourceDestinationMismatchError(),
            422,
            "transfer_source_destination_mismatch",
        ),
    ]
    for failure, status, code in cases:
        service.failure = failure
        response = client.post("/api/v1/transfers", json=payload())
        assert response.status_code == status
        assert response.json()["code"] == code

    invalid = client.post("/api/v1/transfers", json={"source_amount": "0.00"})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "request_invalid"

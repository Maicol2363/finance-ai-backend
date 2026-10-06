"""API integration tests for receipt proposal confirmation endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient

from finance_ai.core.errors import AuthenticationRequiredError
from finance_ai.expenses.dependencies import get_expense_service
from finance_ai.expenses.repositories import (
    CategoryReference,
    CurrencyReference,
    ExpenseRecord,
)
from finance_ai.expenses.service import ExpenseService
from finance_ai.main import create_app
from finance_ai.receipts.dependencies import get_current_user_id

NOW = datetime.now(UTC)
COP = CurrencyReference(id=1, code="COP", name="Colombian Peso", decimals=2)


def make_test_expense_record(
    *,
    expense_id: int = 101,
    transaction_id: int = 202,
    amount: Decimal = Decimal("45000.00"),
    merchant: str = "Éxito Calle 80",
) -> ExpenseRecord:
    return ExpenseRecord(
        id=expense_id,
        transaction_id=transaction_id,
        receipt_id=303,
        name=merchant,
        amount=amount,
        quantity=Decimal("1"),
        unit_price=amount,
        note=None,
        category=CategoryReference(5, "Alimentos"),
        receipt_name=merchant,
        receipt_note=None,
        store=merchant,
        description=merchant,
        occurred_at=NOW,
        status="confirmed",
        capture_source="receipt_photo_ai",
        account_id=1,
        account_name="Principal",
        account_is_active=True,
        currency=COP,
        created_at=NOW,
        updated_at=NOW,
    )


def test_confirm_receipt_proposal_unauthenticated_fails() -> None:
    app = create_app()

    def raise_auth():
        raise AuthenticationRequiredError()

    app.dependency_overrides[get_current_user_id] = raise_auth

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            f"/api/v1/receipts/proposal/{uuid4()}/confirm",
            json={
                "account_id": 1,
                "total_amount": "45000.00",
                "merchant": "Éxito",
                "occurred_at": NOW.isoformat(),
                "currency": "COP",
            },
        )
    assert resp.status_code == 401


def test_confirm_receipt_proposal_success_returns_201() -> None:
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: 100

    mock_service = MagicMock(spec=ExpenseService)
    record = make_test_expense_record()
    mock_service.confirm_receipt_proposal.return_value = (record, True)
    app.dependency_overrides[get_expense_service] = lambda: mock_service

    proposal_id = uuid4()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            f"/api/v1/receipts/proposal/{proposal_id}/confirm",
            json={
                "account_id": 1,
                "total_amount": "45000.00",
                "merchant": "Éxito Calle 80",
                "occurred_at": NOW.isoformat(),
                "currency": "COP",
                "category_id": 5,
                "items": [
                    {
                        "name": "Leche 1L",
                        "total_price": "10000.00",
                        "quantity": "2",
                        "unit_price": "5000.00",
                    },
                    {
                        "name": "Carne Res 1kg",
                        "total_price": "35000.00",
                        "quantity": "1",
                        "unit_price": "35000.00",
                    },
                ],
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == 101
    assert data["amount"] == "45000.00"
    assert data["store"] == "Éxito Calle 80"
    assert data["capture_source"] == "receipt_photo_ai"


def test_confirm_receipt_proposal_idempotent_replay_returns_200() -> None:
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: 100

    mock_service = MagicMock(spec=ExpenseService)
    record = make_test_expense_record()
    mock_service.confirm_receipt_proposal.return_value = (record, False)  # Replay!
    app.dependency_overrides[get_expense_service] = lambda: mock_service

    proposal_id = uuid4()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            f"/api/v1/receipts/proposal/{proposal_id}/confirm",
            json={
                "account_id": 1,
                "total_amount": "45000.00",
                "merchant": "Éxito Calle 80",
                "occurred_at": NOW.isoformat(),
                "currency": "COP",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 101


def test_confirm_receipt_proposal_extra_fields_forbidden() -> None:
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: 100

    proposal_id = uuid4()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            f"/api/v1/receipts/proposal/{proposal_id}/confirm",
            json={
                "account_id": 1,
                "total_amount": "45000.00",
                "merchant": "Éxito Calle 80",
                "occurred_at": NOW.isoformat(),
                "currency": "COP",
                "malicious_extra": "drop database",
            },
        )

    assert resp.status_code == 422


def test_confirm_receipt_proposal_items_max_limit_exceeded() -> None:
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: 100

    # 201 items exceeds max_length=200
    items = [{"name": f"Item {i}", "total_price": "100.00"} for i in range(201)]

    proposal_id = uuid4()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            f"/api/v1/receipts/proposal/{proposal_id}/confirm",
            json={
                "account_id": 1,
                "total_amount": "20100.00",
                "merchant": "Big Store",
                "occurred_at": NOW.isoformat(),
                "currency": "COP",
                "items": items,
            },
        )

    assert resp.status_code == 422


def test_confirm_receipt_proposal_response_includes_items_and_receipt_total() -> None:
    from finance_ai.expenses.repositories import ExpenseItemRecord

    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: 100

    mock_service = MagicMock(spec=ExpenseService)
    item1 = ExpenseItemRecord(
        id=10,
        name="Mezcla Fusion",
        amount=Decimal("5300.00"),
        quantity=Decimal("1"),
        unit_price=Decimal("5300.00"),
        note=None,
        category=CategoryReference(5, "Alimentos"),
    )
    item2 = ExpenseItemRecord(
        id=11,
        name="Galletas",
        amount=Decimal("6400.00"),
        quantity=Decimal("1"),
        unit_price=Decimal("6400.00"),
        note=None,
        category=CategoryReference(5, "Alimentos"),
    )
    record = make_test_expense_record(amount=Decimal("5300.00"), merchant="D1 SAS")
    record_with_items = ExpenseRecord(
        id=record.id,
        transaction_id=record.transaction_id,
        receipt_id=record.receipt_id,
        name=record.name,
        amount=record.amount,
        quantity=record.quantity,
        unit_price=record.unit_price,
        note=record.note,
        category=record.category,
        receipt_name=record.receipt_name,
        receipt_note=record.receipt_note,
        store=record.store,
        description=record.description,
        occurred_at=record.occurred_at,
        status=record.status,
        capture_source=record.capture_source,
        account_id=record.account_id,
        account_name=record.account_name,
        account_is_active=record.account_is_active,
        currency=record.currency,
        created_at=record.created_at,
        updated_at=record.updated_at,
        receipt_total=Decimal("11700.00"),
        items=(item1, item2),
    )
    mock_service.confirm_receipt_proposal.return_value = (record_with_items, True)
    app.dependency_overrides[get_expense_service] = lambda: mock_service

    proposal_id = uuid4()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            f"/api/v1/receipts/proposal/{proposal_id}/confirm",
            json={
                "account_id": 1,
                "total_amount": "11700.00",
                "merchant": "D1 SAS",
                "occurred_at": NOW.isoformat(),
                "currency": "COP",
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["receipt_total"] == "11700.00"
    assert len(data["items"]) == 2
    assert data["items"][0]["name"] == "Mezcla Fusion"
    assert data["items"][0]["amount"] == "5300.00"
    assert data["items"][1]["name"] == "Galletas"
    assert data["items"][1]["amount"] == "6400.00"

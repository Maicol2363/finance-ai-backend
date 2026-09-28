"""Integration tests for Receipt Proposal API endpoints."""

from __future__ import annotations

import io
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from finance_ai.core.errors import AuthenticationRequiredError
from finance_ai.db.session import get_db_session
from finance_ai.main import create_app
from finance_ai.receipts.dependencies import get_current_user_id
from finance_ai.receipts.models import ReceiptProposalModel
from finance_ai.receipts.schemas import RawCandidateItem, RawExtractionDTO


def _make_test_jpeg() -> bytes:
    img = Image.new("RGB", (400, 600), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_btrim(dbapi_connection, connection_record):
        if isinstance(dbapi_connection, sqlite3.Connection):
            dbapi_connection.create_function("btrim", 1, lambda s: s.strip() if s else s)

    ReceiptProposalModel.__table__.create(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as session:
        yield session
    ReceiptProposalModel.__table__.drop(engine)


@pytest.fixture
def client_user_1(db_session: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: 101
    app.dependency_overrides[get_db_session] = lambda: db_session

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def client_user_2(db_session: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: 202
    app.dependency_overrides[get_db_session] = lambda: db_session

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_create_proposal_unauthenticated_fails() -> None:
    app = create_app()

    def raise_auth():
        raise AuthenticationRequiredError()

    app.dependency_overrides[get_current_user_id] = raise_auth

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/v1/receipts/proposal",
            files={"file": ("receipt.jpg", _make_test_jpeg(), "image/jpeg")},
        )
    assert resp.status_code == 401


def test_proposal_lifecycle_create_get_retry_delete(
    client_user_1: TestClient, client_user_2: TestClient
) -> None:
    mock_dto = RawExtractionDTO(
        is_receipt=True,
        merchant_name="Supermercado Éxito",
        purchase_date="2026-09-25",
        total_amount=Decimal("35000.00"),
        currency="COP",
        items=[
            RawCandidateItem(
                name="Leche 1L", quantity=Decimal("2"), total_price=Decimal("10000.00")
            ),
            RawCandidateItem(
                name="Carne 1kg", quantity=Decimal("1"), total_price=Decimal("25000.00")
            ),
        ],
    )

    with patch(
        "finance_ai.receipts.gateway.GeminiVisionAdapter.extract_receipt_dto",
        new=AsyncMock(return_value=(mock_dto, 350.0)),
    ):
        # 1. Create Proposal (POST /proposal)
        resp = client_user_1.post(
            "/api/v1/receipts/proposal",
            files={"file": ("receipt.jpg", _make_test_jpeg(), "image/jpeg")},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["merchant_name"] == "Supermercado Éxito"
        assert data["total_amount"] == "35000.00"
        assert data["revision"] == 1
        assert len(data["items"]) == 2
        proposal_id = data["proposal_id"]

        # 2. Fetch Proposal (GET /proposal/{id})
        get_resp = client_user_1.get(f"/api/v1/receipts/proposal/{proposal_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["proposal_id"] == proposal_id

        # 3. Tenant Isolation: User 2 cannot access User 1's proposal
        isolated_resp = client_user_2.get(f"/api/v1/receipts/proposal/{proposal_id}")
        assert isolated_resp.status_code == 404

        # 4. Retry Proposal with rotation (POST /proposal/{id}/retry)
        retry_dto = RawExtractionDTO(
            is_receipt=True,
            merchant_name="Supermercado Éxito Rotated",
            purchase_date="2026-09-25",
            total_amount=Decimal("35000.00"),
            currency="COP",
            items=[
                RawCandidateItem(
                    name="Leche 1L", quantity=Decimal("2"), total_price=Decimal("10000.00")
                ),
            ],
        )
        with patch(
            "finance_ai.receipts.gateway.GeminiVisionAdapter.extract_receipt_dto",
            new=AsyncMock(return_value=(retry_dto, 400.0)),
        ):
            retry_resp = client_user_1.post(
                f"/api/v1/receipts/proposal/{proposal_id}/retry",
                data={"rotation_degrees": 90},
                files={"file": ("receipt.jpg", _make_test_jpeg(), "image/jpeg")},
            )
            assert retry_resp.status_code == 200
            retry_data = retry_resp.json()
            assert retry_data["revision"] == 2
            assert retry_data["merchant_name"] == "Supermercado Éxito Rotated"

        # 5. Discard Proposal (DELETE /proposal/{id})
        del_resp = client_user_1.delete(f"/api/v1/receipts/proposal/{proposal_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["status"] == "discarded"


def test_create_proposal_detects_duplicate_receipt(client_user_1: TestClient) -> None:
    from unittest.mock import MagicMock
    from finance_ai.expenses.dependencies import get_expense_service
    from finance_ai.expenses.service import ExpenseService

    mock_dto = RawExtractionDTO(
        is_receipt=True,
        merchant_name="D1 SAS",
        purchase_date="2026-09-27",
        total_amount=Decimal("11700.00"),
        currency="COP",
        items=[
            RawCandidateItem(
                name="Mezcla Fusion", quantity=Decimal("1"), total_price=Decimal("5300.00")
            ),
        ],
    )

    mock_service = MagicMock(spec=ExpenseService)
    mock_service.find_potential_duplicate.return_value = True

    app = client_user_1.app
    app.dependency_overrides[get_current_user_id] = lambda: 303
    app.dependency_overrides[get_expense_service] = lambda: mock_service


    with patch(
        "finance_ai.receipts.gateway.GeminiVisionAdapter.extract_receipt_dto",
        new=AsyncMock(return_value=(mock_dto, 350.0)),
    ):
        resp = client_user_1.post(
            "/api/v1/receipts/proposal",
            files={"file": ("receipt.jpg", _make_test_jpeg(), "image/jpeg")},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["possible_duplicate"] is True
        assert "Ya registraste una compra en D1 SAS" in data["duplicate_warning"]
        assert any(i["code"] == "possible_duplicate" for i in data["issues"])


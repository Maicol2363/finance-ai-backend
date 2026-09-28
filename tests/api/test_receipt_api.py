"""Integration tests for Receipt Intelligence API endpoints."""

import io
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

from finance_ai.main import create_app
from finance_ai.receipts.dependencies import get_current_user_id
from finance_ai.receipts.schemas import ReceiptExtraction, ReceiptItem


def test_scan_endpoint_requires_auth() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.post("/api/v1/receipts/scan")
    assert resp.status_code == 401


def test_scan_endpoint_mocked_deepseek() -> None:
    """Tests /api/v1/receipts/scan with an uploaded receipt image."""
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: 1
    client = TestClient(app)

    # Synthetic receipt image
    img = Image.new("RGB", (800, 1600), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    img_bytes = buf.getvalue()

    mock_extraction = ReceiptExtraction(
        merchant="Supermercado Olímpica",
        occurred_at="2026-09-23",
        printed_total=Decimal("54000.00"),
        currency="COP",
        items=[
            ReceiptItem(
                name="Arroz 5kg",
                quantity=Decimal("1"),
                unit_price=Decimal("24000.00"),
                amount=Decimal("24000.00"),
            ),
            ReceiptItem(
                name="Aceite 1L",
                quantity=Decimal("2"),
                unit_price=Decimal("15000.00"),
                amount=Decimal("30000.00"),
            ),
        ],
    )

    with patch(
        "finance_ai.receipts.deepseek_client.DeepSeekVisionService.extract_receipt",
        new=AsyncMock(return_value=(mock_extraction, 450.0)),
    ):
        resp = client.post(
            "/api/v1/receipts/scan",
            files={"file": ("receipt.jpg", img_bytes, "image/jpeg")},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["extraction"]["merchant"] == "Supermercado Olímpica"
    assert data["extraction"]["printed_total"] == "54000.00"
    assert data["extraction"]["is_reconciled"] is True
    assert data["optimization"]["width"] == 768  # 768px tile boundary clamp!
    assert data["ai_latency_ms"] == 450.0

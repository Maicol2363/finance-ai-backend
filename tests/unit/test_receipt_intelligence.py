"""Unit tests for HU-015 Receipt Intelligence & Optimizations."""

import io
from decimal import Decimal

from PIL import Image

from finance_ai.receipts.dian_resolver import DianResolverService
from finance_ai.receipts.preprocessor import preprocess_receipt_image
from finance_ai.receipts.schemas import ReceiptExtraction, ReceiptItem


def test_tile_clamping_to_768px() -> None:
    """Verifies that large receipt photos are clamped strictly to 768px width (1 ViT tile wide)."""
    # Create synthetic 1600x3200 receipt image
    img = Image.new("RGB", (1600, 3200), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    raw_bytes = buf.getvalue()

    comp_bytes, meta = preprocess_receipt_image(raw_bytes, clamp_width=768, quality=75)

    assert meta.width == 768
    assert meta.height == 1536
    assert meta.savings_percent > 50.0
    assert meta.tile_count == 2  # Exactly 2 vertical tiles of 768x768!
    assert len(comp_bytes) < len(raw_bytes)


def test_fiscal_reconciliation_exact_match() -> None:
    """Verifies that mathematical reconciliation passes when items match the printed total."""
    extraction = ReceiptExtraction(
        merchant="Éxito Calle 80",
        printed_total=Decimal("45000.00"),
        currency="COP",
        items=[
            ReceiptItem(
                name="Leche 1L",
                quantity=Decimal("2"),
                unit_price=Decimal("5000.00"),
                amount=Decimal("10000.00"),
            ),
            ReceiptItem(
                name="Carne Res 1kg",
                quantity=Decimal("1"),
                unit_price=Decimal("35000.00"),
                amount=Decimal("35000.00"),
            ),
        ],
    )
    assert extraction.is_reconciled is True
    assert "matches total" in (extraction.reconciliation_notes or "")


def test_fiscal_reconciliation_detects_discrepancy() -> None:
    """Verifies that mathematical discrepancy is flagged when items do not add up to total."""
    extraction = ReceiptExtraction(
        merchant="Restaurante El Portal",
        printed_total=Decimal("100000.00"),
        currency="COP",
        items=[
            ReceiptItem(
                name="Almuerzo Ejecutivo",
                quantity=Decimal("1"),
                unit_price=Decimal("25000.00"),
                amount=Decimal("25000.00"),
            ),
        ],
    )
    assert extraction.is_reconciled is False
    assert "differs from printed total" in (extraction.reconciliation_notes or "")


def test_dian_cufe_validation() -> None:
    """Verifies that non-64-hex strings are rejected immediately without network calls."""
    resolver = DianResolverService()
    import asyncio

    res, _lat, msg = asyncio.run(resolver.resolve_cufe("not-a-valid-cufe"))
    assert res is None
    assert msg == "INVALID_CUFE_FORMAT"

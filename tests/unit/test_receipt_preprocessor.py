"""Unit tests for ephemeral receipt image preprocessing worker."""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from PIL import Image

from finance_ai.receipts.preprocessor import (
    MAX_DIMENSION,
    _process_image_sync,
    preprocess_receipt_image_ephemeral,
)


def _make_test_image(
    width: int, height: int, color: tuple[int, int, int] = (255, 255, 255)
) -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_process_image_normalizes_and_strips_metadata() -> None:
    raw = _make_test_image(800, 1200)
    compressed, meta = _process_image_sync(raw, quality=85)

    assert meta.width == 800
    assert meta.height == 1200
    assert len(compressed) > 0

    with Image.open(io.BytesIO(compressed)) as out_img:
        assert out_img.format == "JPEG"
        # No EXIF dictionary or empty
        exif = out_img.getexif()
        assert len(exif) == 0


def test_process_image_resizes_oversized_images() -> None:
    # 4000x2000 image should be resized so max dimension is <= 2400
    raw = _make_test_image(4000, 2000)
    _, meta = _process_image_sync(raw)

    assert meta.width == 2400
    assert meta.height == 1200
    assert max(meta.width, meta.height) <= 2400


def test_process_image_rotation_90() -> None:
    raw = _make_test_image(800, 400)
    _, meta = _process_image_sync(raw, rotation_degrees=90)

    # 800x400 rotated 90 becomes 400x800
    assert meta.width == 400
    assert meta.height == 800


def test_process_image_exceeds_max_dimension() -> None:
    raw = _make_test_image(MAX_DIMENSION + 10, 10)
    with pytest.raises(HTTPException) as exc_info:
        _process_image_sync(raw)
    assert exc_info.value.status_code == 422


@pytest.mark.anyio
async def test_preprocess_receipt_image_ephemeral_async() -> None:
    raw = _make_test_image(600, 800)
    compressed, meta = await preprocess_receipt_image_ephemeral(raw, quality=85)
    assert meta.width == 600
    assert meta.height == 800
    assert len(compressed) > 0


def test_decode_failure_returns_a_safe_detail() -> None:
    """Client-visible decode errors must not echo the decoder exception (Codex review #2 family)."""
    corrupted = b"\xff\xd8\xff\xe0" + b"\x00" * 64  # JPEG magic header, undecodable body

    with pytest.raises(HTTPException) as exc_info:
        _process_image_sync(corrupted, quality=85)

    assert exc_info.value.status_code == 422
    assert "Failed to decode image" in exc_info.value.detail
    assert "cannot identify" not in exc_info.value.detail.lower()
    assert "bytesio" not in exc_info.value.detail.lower()

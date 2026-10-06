"""Unit tests for receipt ingress guards, byte bounds, and rate limits."""

from __future__ import annotations

import io
from uuid import uuid4

import pytest
from fastapi import HTTPException

from finance_ai.receipts.guards import (
    CountingStreamReader,
    ReceiptRateLimiter,
    validate_image_magic_header,
)


class AsyncBytesStream:
    """Mock async stream reader."""

    def __init__(self, data: bytes) -> None:
        self.stream = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


@pytest.mark.anyio
async def test_counting_stream_reader_within_limit() -> None:
    data = b"X" * 1024
    reader = CountingStreamReader(AsyncBytesStream(data), max_bytes=2048)
    read_bytes = await reader.read_bounded()
    assert read_bytes == data
    assert reader.bytes_read == 1024


@pytest.mark.anyio
async def test_counting_stream_reader_exceeds_limit() -> None:
    data = b"X" * 3000
    reader = CountingStreamReader(AsyncBytesStream(data), max_bytes=2048)
    with pytest.raises(HTTPException) as exc_info:
        await reader.read_bounded(chunk_size=512)
    assert exc_info.value.status_code == 413
    assert "exceeds maximum" in exc_info.value.detail


@pytest.mark.anyio
async def test_counting_stream_reader_empty_raises_400() -> None:
    reader = CountingStreamReader(AsyncBytesStream(b""), max_bytes=2048)
    with pytest.raises(HTTPException) as exc_info:
        await reader.read_bounded()
    assert exc_info.value.status_code == 400
    assert "empty" in exc_info.value.detail.lower()


def test_validate_image_magic_header_jpeg() -> None:
    jpeg_header = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01])
    assert validate_image_magic_header(jpeg_header) == "jpeg"


def test_validate_image_magic_header_png() -> None:
    png_header = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D])
    assert validate_image_magic_header(png_header) == "png"


def test_validate_image_magic_header_webp() -> None:
    webp_header = b"RIFF" + bytes(4) + b"WEBPVP8 "
    assert validate_image_magic_header(webp_header) == "webp"


def test_validate_image_magic_header_heic() -> None:
    heic_header = bytes(4) + b"ftypheic" + bytes(4)
    assert validate_image_magic_header(heic_header) == "heic"


def test_validate_image_magic_header_invalid_raises_422() -> None:
    invalid_header = bytes([0x4D, 0x5A, 0x90, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00])
    with pytest.raises(HTTPException) as exc_info:
        validate_image_magic_header(invalid_header)
    assert exc_info.value.status_code == 422


def test_validate_image_magic_header_too_short_raises_422() -> None:
    with pytest.raises(HTTPException) as exc_info:
        validate_image_magic_header(b"SHORT")
    assert exc_info.value.status_code == 422


def test_receipt_rate_limiter_burst_and_rejection() -> None:
    limiter = ReceiptRateLimiter(capacity=2.0, requests_per_minute=5.0)
    user_a = uuid4()
    user_b = uuid4()

    # User A consumes burst capacity (2 requests)
    limiter.check_rate_limit(user_a)
    limiter.check_rate_limit(user_a)

    # 3rd request from User A should be rate limited (HTTP 429)
    with pytest.raises(HTTPException) as exc_info:
        limiter.check_rate_limit(user_a)
    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers

    # User B has an independent bucket and should succeed
    limiter.check_rate_limit(user_b)

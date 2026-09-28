"""Ingress guards, resource bounds, and rate limiters for receipt scanning."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from fastapi import HTTPException, UploadFile, status

# 10 MiB limit in bytes
MAX_RECEIPT_IMAGE_BYTES = 10 * 1024 * 1024  # 10,485,760 bytes

# Magic bytes signatures for supported image codecs
JPEG_MAGIC = bytes([0xFF, 0xD8, 0xFF])
PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47])
RIFF_MAGIC = b"RIFF"
WEBP_MAGIC = b"WEBP"

HEIC_BRANDS = {
    b"heic",
    b"heix",
    b"heim",
    b"heis",
    b"mif1",
    b"msf1",
    b"avif",
    b"avis",
}


class CountingStreamReader:
    """Stream reader wrapper enforcing a hard byte limit during chunked upload reading."""

    def __init__(self, stream: Any, max_bytes: int = MAX_RECEIPT_IMAGE_BYTES) -> None:
        self.stream = stream
        self.max_bytes = max_bytes
        self.bytes_read = 0

    async def read_bounded(self, chunk_size: int = 64 * 1024) -> bytes:
        """Reads entire stream into memory while actively enforcing max_bytes limit."""
        buffer = bytearray()
        while True:
            chunk = await self.stream.read(chunk_size)
            if not chunk:
                break
            self.bytes_read += len(chunk)
            if self.bytes_read > self.max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Receipt image exceeds maximum 10 MB limit.",
                )
            buffer.extend(chunk)

        if not buffer:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty.",
            )
        return bytes(buffer)


async def read_and_validate_stream(
    file: UploadFile, max_bytes: int = MAX_RECEIPT_IMAGE_BYTES
) -> bytes:
    """Reads uploaded file bytes while enforcing Content-Length and streaming limits."""
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Receipt image exceeds maximum 10 MB limit.",
        )

    reader = CountingStreamReader(file, max_bytes=max_bytes)
    return await reader.read_bounded()


def validate_image_magic_header(header_bytes: bytes) -> str:
    """Validates initial image header bytes against allowed magic numbers.

    Supported formats: JPEG, PNG, WEBP, and HEIC/HEIF/AVIF.
    Raises HTTP 422 if the magic header is unrecognized or invalid.
    """
    if len(header_bytes) < 12:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unsupported or invalid image format. Supported formats: JPEG, PNG, HEIC, WEBP.",
        )

    # Check JPEG
    if header_bytes.startswith(JPEG_MAGIC):
        return "jpeg"

    # Check PNG
    if header_bytes.startswith(PNG_MAGIC):
        return "png"

    # Check WEBP: RIFF....WEBP
    if header_bytes.startswith(RIFF_MAGIC) and header_bytes[8:12] == WEBP_MAGIC:
        return "webp"

    # Check HEIC/HEIF/AVIF via ISO BMFF ftyp box
    # Typically offset 4 has 'ftyp' followed by major brand at offset 8
    if len(header_bytes) >= 12 and header_bytes[4:8] == b"ftyp":
        brand = header_bytes[8:12].lower()
        if brand in HEIC_BRANDS:
            return "heic"

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Unsupported or invalid image format. Supported formats: JPEG, PNG, HEIC, WEBP.",
    )


class ReceiptRateLimiter:
    """In-memory token bucket rate limiter per user.

    Enforces 5 requests per minute with a burst capacity of 2 requests.
    """

    def __init__(
        self,
        capacity: float = 2.0,
        requests_per_minute: float = 5.0,
        cleanup_interval_seconds: float = 600.0,
    ) -> None:
        self.capacity = capacity
        self.refill_rate = requests_per_minute / 60.0  # tokens per second
        self.cleanup_interval = cleanup_interval_seconds
        # user_id -> (tokens: float, last_timestamp: float)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._last_cleanup = time.monotonic()

    def _cleanup_old_buckets(self, now: float) -> None:
        """Prunes buckets that have been inactive longer than cleanup interval."""
        if now - self._last_cleanup < self.cleanup_interval:
            return
        self._last_cleanup = now
        stale_threshold = now - self.cleanup_interval
        stale_keys = [
            uid for uid, (_, last_time) in self._buckets.items() if last_time < stale_threshold
        ]
        for uid in stale_keys:
            self._buckets.pop(uid, None)

    def check_rate_limit(self, user_id: UUID | str | int) -> None:
        """Verifies and consumes a token for user_id.

        Raises HTTP 429 if the user has exceeded their rate limit.
        """
        key = str(user_id)
        now = time.monotonic()
        self._cleanup_old_buckets(now)

        current_tokens, last_time = self._buckets.get(key, (self.capacity, now))
        elapsed = max(0.0, now - last_time)
        tokens = min(self.capacity, current_tokens + elapsed * self.refill_rate)

        if tokens < 1.0:
            retry_after = int(max(1.0, (1.0 - tokens) / self.refill_rate))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Rate limit exceeded. Please wait a moment before uploading another receipt."
                ),
                headers={"Retry-After": str(retry_after)},
            )

        self._buckets[key] = (tokens - 1.0, now)


_global_rate_limiter = ReceiptRateLimiter()


def get_receipt_rate_limiter() -> ReceiptRateLimiter:
    return _global_rate_limiter

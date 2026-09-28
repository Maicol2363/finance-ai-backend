"""Ephemeral in-memory image preprocessing, security limits, and optimization worker."""

from __future__ import annotations

import asyncio
import io
import time
from typing import Literal

from fastapi import HTTPException, status
from PIL import Image, ImageOps

from finance_ai.core.logging import get_logger
from finance_ai.receipts.schemas import ImageOptimizationMeta

logger = get_logger(__name__)

# Security limits against decompression bombs and excessive raster sizes
MAX_IMAGE_PIXELS = 20_000_000
MAX_DIMENSION = 12_000
MAX_NORMALIZED_DIMENSION = 2_400
DEFAULT_JPEG_QUALITY = 85

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

_image_worker_semaphore = asyncio.Semaphore(1)


def _process_image_sync(
    image_bytes: bytes,
    *,
    rotation_degrees: Literal[0, 90, 180, 270] | int = 0,
    clamp_width: int | None = None,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> tuple[bytes, ImageOptimizationMeta]:
    """Synchronous CPU-bound image normalization and compression worker."""
    start = time.perf_counter()
    orig_bytes_len = len(image_bytes)

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            # Dimension checks
            if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"Image dimensions ({img.width}x{img.height}) "
                        f"exceed maximum allowed {MAX_DIMENSION} px limit."
                    ),
                )

            # EXIF orientation normalization
            img = ImageOps.exif_transpose(img)

            # Manual rotation (client-driven retry: 90, 180, 270 clockwise)
            if rotation_degrees in (90, 180, 270):
                # Pillow rotate rotates counter-clockwise; negative rotates clockwise
                img = img.rotate(-rotation_degrees, expand=True)

            # Strip EXIF metadata and normalize color channels to standard RGB
            if img.mode != "RGB":
                img = img.convert("RGB")

            orig_w, orig_h = img.size

            # Proportional downscaling
            if clamp_width is not None and orig_w > clamp_width:
                scale = clamp_width / orig_w
                new_w = clamp_width
                new_h = max(1, int(orig_h * scale))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            else:
                max_dim = max(orig_w, orig_h)
                if max_dim > MAX_NORMALIZED_DIMENSION:
                    scale = MAX_NORMALIZED_DIMENSION / max_dim
                    new_w = max(1, int(orig_w * scale))
                    new_h = max(1, int(orig_h * scale))
                    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                else:
                    new_w, new_h = orig_w, orig_h

            # Encode to JPEG with stripped metadata
            out_buffer = io.BytesIO()
            img.save(
                out_buffer,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=False,
            )
            compressed_bytes = out_buffer.getvalue()
    except Image.DecompressionBombError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Image exceeds pixel limit (potential decompression bomb detected).",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Decoder messages can echo file contents or parser internals, so only the failure type
        # is recorded and the client receives a fixed safe detail (AGENTS.md privacy contract).
        logger.warning(
            "Receipt image decode failed",
            extra={
                "event": "receipt.preprocess.decode_failed",
                "outcome": "invalid_input",
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Failed to decode image. Use a valid JPEG, PNG, HEIC or WEBP file.",
        ) from exc

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    comp_len = len(compressed_bytes)
    savings = round((1.0 - (comp_len / orig_bytes_len)) * 100.0, 1) if orig_bytes_len > 0 else 0.0

    tile_cols = max(1, (new_w + 767) // 768)
    tile_rows = max(1, (new_h + 767) // 768)
    tile_count = tile_cols * tile_rows

    meta = ImageOptimizationMeta(
        original_bytes=orig_bytes_len,
        compressed_bytes=comp_len,
        savings_percent=savings,
        compression_time_ms=duration_ms,
        width=new_w,
        height=new_h,
        tile_count=tile_count,
    )

    return compressed_bytes, meta


async def preprocess_receipt_image_ephemeral(
    image_bytes: bytes,
    *,
    rotation_degrees: Literal[0, 90, 180, 270] | int = 0,
    clamp_width: int | None = None,
    quality: int = DEFAULT_JPEG_QUALITY,
    timeout_seconds: float = 10.0,
) -> tuple[bytes, ImageOptimizationMeta]:
    """Asynchronous entrypoint executing image preprocessing under semaphore and bounded timeout."""
    async with _image_worker_semaphore:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _process_image_sync,
                    image_bytes,
                    rotation_degrees=rotation_degrees,
                    clamp_width=clamp_width,
                    quality=quality,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_408_REQUEST_TIMEOUT,
                detail="Image preprocessing timed out after 10 seconds.",
            ) from None


def preprocess_receipt_image(
    image_bytes: bytes,
    *,
    clamp_width: int = 768,
    quality: int = 75,
) -> tuple[bytes, ImageOptimizationMeta]:
    """Synchronous legacy compatibility helper for clamping to 768px."""
    return _process_image_sync(image_bytes, clamp_width=clamp_width, quality=quality)

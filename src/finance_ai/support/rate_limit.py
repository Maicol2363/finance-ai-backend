"""Bound support requests per peer using the project's in-process limiter convention."""

from functools import lru_cache

from fastapi import Request
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from finance_ai.core.errors import AppError


@lru_cache
def support_limiter() -> FixedWindowRateLimiter:
    return FixedWindowRateLimiter(MemoryStorage())


def enforce_support_limit(request: Request) -> None:
    peer = request.client.host if request.client else "unknown"
    if not support_limiter().hit(parse("120/minute"), "support", peer):
        raise AppError(
            status=429,
            code="support_rate_limited",
            title="Too many support requests",
            detail="Please wait before trying again.",
            headers={"Retry-After": "60"},
        )

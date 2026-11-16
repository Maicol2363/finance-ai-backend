"""Internal endpoint a scheduler calls to send what is waiting in the outbox.

Cloud Run only guarantees CPU while a request is being served, so delivery cannot depend on a
background thread finishing after a response. A scheduler (Cloud Scheduler) posts here every
minute with a shared secret; the outbox makes the sweep safe to run on several instances. The
route is deliberately absent from the public OpenAPI contract.
"""

from __future__ import annotations

import hmac
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from pydantic import BaseModel

from finance_ai.core.config import Settings, get_settings
from finance_ai.core.errors import AppError
from finance_ai.db.session import get_session_factory
from finance_ai.notifications.dispatcher import NotificationDispatcher
from finance_ai.notifications.senders import create_email_sender

router = APIRouter(prefix="/internal/notifications", include_in_schema=False)


class DispatchResponse(BaseModel):
    sent: int
    skipped: int
    retried: int
    failed: int


@lru_cache
def _limiter() -> FixedWindowRateLimiter:
    return FixedWindowRateLimiter(MemoryStorage())


def get_dispatcher() -> NotificationDispatcher:
    settings = get_settings()
    return NotificationDispatcher(
        session_factory=get_session_factory(),
        sender=create_email_sender(settings),
        settings=settings,
    )


def _authorize(settings: Settings, presented: str | None, request: Request) -> None:
    token = settings.notification_dispatch_token
    if token is None:
        raise AppError(
            status=503,
            code="notification_dispatch_disabled",
            title="Notification dispatch is not enabled",
            detail="This endpoint is not configured.",
        )
    peer = request.client.host if request.client else "unknown"
    if not _limiter().hit(parse("30/minute"), "notification-dispatch", peer):
        raise AppError(
            status=429,
            code="notification_dispatch_rate_limited",
            title="Too many dispatch requests",
            detail="Please wait before trying again.",
            headers={"Retry-After": "60"},
        )
    expected = token.get_secret_value().encode()
    if presented is None or not hmac.compare_digest(presented.encode(), expected):
        raise AppError(
            status=401,
            code="dispatch_token_invalid",
            title="Dispatch token is not valid",
            detail="The dispatch credential is missing or invalid.",
        )


@router.post("/dispatch", response_model=DispatchResponse)
def dispatch_notifications(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    x_dispatch_token: Annotated[str | None, Header()] = None,
) -> DispatchResponse:
    _authorize(settings, x_dispatch_token, request)
    dispatcher = get_dispatcher()
    summary = dispatcher.dispatch_once()
    return DispatchResponse(
        sent=summary.sent,
        skipped=summary.skipped,
        retried=summary.retried,
        failed=summary.failed,
    )

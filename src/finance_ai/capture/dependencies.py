"""Wiring of the text capture service and its per-user request budget."""

from __future__ import annotations

from fastapi import HTTPException

from finance_ai.accounts.dependencies import get_account_service
from finance_ai.capture.context import AccountEntry, CaptureContext, CatalogEntry
from finance_ai.capture.gateway import get_text_gateway
from finance_ai.capture.service import TextCaptureService
from finance_ai.categories.dependencies import get_category_service
from finance_ai.core.errors import TextCaptureRateLimitedError
from finance_ai.db.session import get_session_factory
from finance_ai.expenses.dependencies import get_expense_service
from finance_ai.income_sources.dependencies import get_income_source_service
from finance_ai.incomes.dependencies import get_income_service
from finance_ai.receipts.guards import ReceiptRateLimiter
from finance_ai.transfers.dependencies import get_transfer_service

# A short burst for a chat, then a steady pace: every typed message costs one AI call.
# Tapped options are answered without the model, so they never spend this budget.
_rate_limiter = ReceiptRateLimiter(capacity=10.0, requests_per_minute=20.0)


def load_capture_context(owner_id: int) -> CaptureContext:
    """The caller's active accounts and catalogs, the only values a proposal may reference."""
    accounts = get_account_service().list(
        owner_id=owner_id, preferred_currency_code="COP", include_inactive=False
    )
    categories = get_category_service().list(owner_id=owner_id)
    sources = get_income_source_service().list(user_id=owner_id)
    return CaptureContext(
        accounts=tuple(
            AccountEntry(id=a.id, name=a.name, currency_code=a.currency.code)
            for a in accounts.accounts
            if a.is_active
        ),
        categories=tuple(CatalogEntry(id=c.id, name=c.name) for c in categories.categories),
        income_sources=tuple(CatalogEntry(id=s.id, name=s.name) for s in sources),
    )


def get_text_capture_service() -> TextCaptureService:
    return TextCaptureService(
        session_factory=get_session_factory(),
        gateway=get_text_gateway(),
        context_loader=load_capture_context,
        expenses=get_expense_service(),
        incomes=get_income_service(),
        transfers=get_transfer_service(),
    )


def enforce_text_capture_rate_limit(user_id: int) -> None:
    try:
        _rate_limiter.check_rate_limit(user_id)
    except HTTPException as exc:
        retry_after = int((exc.headers or {}).get("Retry-After", "5"))
        raise TextCaptureRateLimitedError(retry_after) from None

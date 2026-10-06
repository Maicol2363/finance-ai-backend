"""FastAPI dependencies for receipt scanning and proposal management."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import Depends

from finance_ai.auth.dependencies import get_session_service, require_bearer_access
from finance_ai.auth.session_service import SessionService
from finance_ai.db.expense_uow import SQLAlchemyExpenseUnitOfWork
from finance_ai.db.receipt_uow import SQLAlchemyReceiptProposalUnitOfWork
from finance_ai.db.session import get_session_factory
from finance_ai.receipts.service import ReceiptProposalService
from finance_ai.receipts.uow import ReceiptProposalUnitOfWork


def get_current_user_id(
    access_credential: Annotated[str, Depends(require_bearer_access)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> int:
    """Authenticates request via Bearer token and returns integer user_id."""
    return session_service.current_user(access_credential).user_id


def _find_potential_duplicate(
    owner_id: int, merchant: str, total_amount: Decimal, occurred_date: date
) -> bool:
    """Reads duplicate candidates through the expense unit of work."""
    with SQLAlchemyExpenseUnitOfWork(get_session_factory()) as uow:
        return uow.expenses.find_potential_duplicate(
            owner_id=owner_id,
            merchant=merchant,
            total_amount=total_amount,
            occurred_date=occurred_date,
        )


def get_receipt_proposal_service() -> ReceiptProposalService:
    def uow_factory() -> ReceiptProposalUnitOfWork:
        return SQLAlchemyReceiptProposalUnitOfWork(get_session_factory())

    return ReceiptProposalService(
        uow_factory=uow_factory,
        duplicate_lookup=_find_potential_duplicate,
    )

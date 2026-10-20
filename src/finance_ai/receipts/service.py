"""Application service owning the receipt proposal draft lifecycle and its transactions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from finance_ai.core.errors import ReceiptProposalNotFoundError
from finance_ai.core.logging import get_logger
from finance_ai.receipts.schemas import ReceiptProposalResponse, ValidationIssue
from finance_ai.receipts.uow import ReceiptProposalUnitOfWork

logger = get_logger(__name__)

ReceiptProposalUnitOfWorkFactory = Callable[[], ReceiptProposalUnitOfWork]
# (owner_id, merchant, total_amount, occurred_date) -> a likely duplicate already exists
DuplicateLookup = Callable[[int, str, Decimal, date], bool]


class ReceiptProposalService:
    """Owns draft staging, duplicate flagging, commits and not-found semantics.

    Transactions belong to this layer so the HTTP router never manages a session directly
    (AGENTS.md architecture rule) and proposal writes can grow related work atomically.
    """

    def __init__(
        self,
        *,
        uow_factory: ReceiptProposalUnitOfWorkFactory,
        duplicate_lookup: DuplicateLookup | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.duplicate_lookup = duplicate_lookup

    def create_draft(
        self, *, owner_id: int, proposal: ReceiptProposalResponse
    ) -> ReceiptProposalResponse:
        """Stores a new draft for the owner and returns it with any duplicate hint applied."""
        flagged = self._apply_duplicate_flag(owner_id=owner_id, proposal=proposal)
        with self.uow_factory() as uow:
            uow.proposals.create(owner_id, flagged)
            uow.commit()
        return flagged

    def get_draft(self, *, owner_id: int, proposal_id: UUID) -> ReceiptProposalResponse:
        """Returns the owner's draft, raising a not-found error when it is missing or expired."""
        with self.uow_factory() as uow:
            row = uow.proposals.get_by_id_for_user(owner_id, proposal_id)
            if row is None:
                raise ReceiptProposalNotFoundError
            # Read the stored payload while the session is still open: closing the unit of work
            # expires the ORM instance.
            stored = dict(row.proposal_data)
            stored.pop("transaction_id", None)
            stored["status"] = row.status
        return ReceiptProposalResponse.model_validate(stored)

    def replace_draft(
        self,
        *,
        owner_id: int,
        proposal_id: UUID,
        proposal: ReceiptProposalResponse,
    ) -> ReceiptProposalResponse:
        """Overwrites an existing draft (retry/rotation) and returns the stored proposal."""
        flagged = self._apply_duplicate_flag(owner_id=owner_id, proposal=proposal)
        with self.uow_factory() as uow:
            updated = uow.proposals.update(owner_id, proposal_id, flagged)
            if updated is None:
                raise ReceiptProposalNotFoundError
            if updated.status in {"confirmed", "discarded"}:
                stored = dict(updated.proposal_data)
                stored.pop("transaction_id", None)
                stored["status"] = updated.status
                flagged = ReceiptProposalResponse.model_validate(stored)
            uow.commit()
        return flagged

    def discard_draft(self, *, owner_id: int, proposal_id: UUID) -> None:
        """Marks a draft as discarded, raising a not-found error when the owner has none."""
        with self.uow_factory() as uow:
            if not uow.proposals.discard(owner_id, proposal_id):
                raise ReceiptProposalNotFoundError
            uow.commit()

    def _apply_duplicate_flag(
        self, *, owner_id: int, proposal: ReceiptProposalResponse
    ) -> ReceiptProposalResponse:
        """Adds a duplicate warning when an equivalent expense already exists."""
        if self.duplicate_lookup is None:
            return proposal
        if not proposal.merchant_name or not proposal.total_amount:
            return proposal

        occurred_date = datetime.now(UTC).date()
        if proposal.purchase_date:
            try:
                occurred_date = datetime.strptime(proposal.purchase_date, "%Y-%m-%d").date()
            except ValueError:
                occurred_date = datetime.now(UTC).date()

        try:
            is_duplicate = self.duplicate_lookup(
                owner_id, proposal.merchant_name, proposal.total_amount, occurred_date
            )
        except Exception as exc:
            logger.warning(
                "Duplicate pre-check skipped for a receipt proposal",
                extra={
                    "event": "receipt.duplicate_check.failed",
                    "outcome": "degraded",
                    "error_type": type(exc).__name__,
                },
            )
            return proposal
        if not is_duplicate:
            return proposal

        warning = (
            f"Ya registraste una compra en {proposal.merchant_name} "
            f"por ${proposal.total_amount:,.2f} en esta fecha."
        )
        return proposal.model_copy(
            update={
                "possible_duplicate": True,
                "duplicate_warning": warning,
                "issues": [
                    *proposal.issues,
                    ValidationIssue(
                        code="possible_duplicate",
                        message=warning,
                        severity="warning",
                    ),
                ],
            }
        )

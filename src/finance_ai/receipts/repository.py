"""Repository for managing temporary receipt proposal drafts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from finance_ai.receipts.models import ReceiptProposalModel
from finance_ai.receipts.schemas import ReceiptProposalResponse


class ReceiptProposalRepository:
    """Manages receipt proposal persistence with strict user isolation."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def purge_expired(self, cutoff: datetime | None = None) -> int:
        """Purges proposals that have expired or been completed/discarded beyond 24h."""
        now = cutoff or datetime.now(UTC)
        stmt = delete(ReceiptProposalModel).where(
            (ReceiptProposalModel.expires_at < now)
            | (
                ReceiptProposalModel.status.in_(["confirmed", "discarded"])
                & (ReceiptProposalModel.created_at < now - timedelta(hours=24))
            )
        )
        res = self.session.execute(stmt)
        self.session.flush()
        return res.rowcount or 0

    def create(self, user_id: int, proposal: ReceiptProposalResponse) -> ReceiptProposalModel:
        """Stores a new receipt proposal draft and opportunistically purges expired drafts."""
        try:
            self.purge_expired()
        except Exception:
            pass

        data = proposal.model_dump(mode="json")
        row = ReceiptProposalModel(
            id=proposal.proposal_id,
            user_id=user_id,
            status=proposal.status,
            proposal_data=data,
            revision=proposal.revision,
            created_at=proposal.created_at,
            expires_at=proposal.expires_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_by_id_for_user(
        self, user_id: int, proposal_id: UUID, *, for_update: bool = False
    ) -> ReceiptProposalModel | None:
        """Retrieves a proposal ensuring user ownership, optionally with row-level lock."""
        stmt = select(ReceiptProposalModel).where(
            ReceiptProposalModel.id == proposal_id,
            ReceiptProposalModel.user_id == user_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalar(stmt)

    def update(
        self, user_id: int, proposal_id: UUID, proposal: ReceiptProposalResponse
    ) -> ReceiptProposalModel | None:
        """Updates an existing proposal with new revision and data."""
        row = self.get_by_id_for_user(user_id, proposal_id)
        if row is None:
            return None
        row.status = proposal.status
        row.revision = proposal.revision
        row.proposal_data = proposal.model_dump(mode="json")
        row.expires_at = proposal.expires_at
        self.session.flush()
        return row

    def mark_confirmed(
        self, user_id: int, proposal_id: UUID, *, transaction_id: int | None = None
    ) -> bool:
        """Marks a proposal as confirmed and records the associated transaction_id."""
        row = self.get_by_id_for_user(user_id, proposal_id)
        if row is None:
            return False
        row.status = "confirmed"
        if transaction_id is not None:
            data = dict(row.proposal_data)
            data["transaction_id"] = transaction_id
            row.proposal_data = data
        self.session.flush()
        return True

    def discard(self, user_id: int, proposal_id: UUID) -> bool:
        """Marks a proposal as discarded."""
        row = self.get_by_id_for_user(user_id, proposal_id)
        if row is None:
            return False
        row.status = "discarded"
        self.session.flush()
        return True

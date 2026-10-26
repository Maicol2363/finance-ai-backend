"""Draft storage for text capture proposals, always scoped to the authenticated owner."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from finance_ai.capture.models import TextCaptureProposalModel
from finance_ai.capture.schemas import TextProposalResponse
from finance_ai.db.models.transactions import FinancialTransaction


class CaptureOperationRepository:
    """Looks up a confirmed operation by its capture idempotency key."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_confirmed(self, owner_id: int, proposal_id: UUID) -> tuple[int, str] | None:
        row = self.session.execute(
            select(FinancialTransaction.id, FinancialTransaction.transaction_type).where(
                FinancialTransaction.user_id == owner_id,
                FinancialTransaction.source_proposal_id == proposal_id,
                FinancialTransaction.status == "confirmed",
            )
        ).one_or_none()
        return None if row is None else (int(row[0]), str(row[1]))


class TextProposalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def purge_expired(self, now: datetime) -> int:
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                delete(TextCaptureProposalModel).where(TextCaptureProposalModel.expires_at < now)
            ),
        )
        return result.rowcount or 0

    def add(
        self, owner_id: int, proposal: TextProposalResponse, meta: dict[str, Any]
    ) -> TextCaptureProposalModel:
        row = TextCaptureProposalModel(
            id=proposal.proposal_id,
            user_id=owner_id,
            status=proposal.status.value,
            proposal_data={"proposal": proposal.model_dump(mode="json"), "meta": meta},
            revision=proposal.revision,
            created_at=proposal.created_at,
            expires_at=proposal.expires_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(
        self, owner_id: int, proposal_id: UUID, *, now: datetime, for_update: bool = False
    ) -> TextCaptureProposalModel | None:
        """Returns the owner's unexpired draft; foreign, missing and expired ids look the same."""
        statement = select(TextCaptureProposalModel).where(
            TextCaptureProposalModel.id == proposal_id,
            TextCaptureProposalModel.user_id == owner_id,
            TextCaptureProposalModel.expires_at >= now,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def update(
        self,
        row: TextCaptureProposalModel,
        proposal: TextProposalResponse,
        meta: dict[str, Any],
        *,
        confirmation: dict[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {"proposal": proposal.model_dump(mode="json"), "meta": meta}
        if confirmation is not None:
            data["confirmation"] = confirmation
        row.status = proposal.status.value
        row.revision = proposal.revision
        row.expires_at = proposal.expires_at
        row.proposal_data = data
        self.session.flush()

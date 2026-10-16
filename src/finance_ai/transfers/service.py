"""Atomic transfer application service for HU-010."""

from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from finance_ai.core.errors import (
    TransferAccountUnavailableError,
    TransferDestinationAmountRequiredError,
    TransferSameAccountError,
    TransferSourceDestinationMismatchError,
)
from finance_ai.transfers.repository import TransferRepository
from finance_ai.transfers.schemas import TransferCreateRequest, TransferRecord


class TransferService:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def create(
        self,
        *,
        owner_id: int,
        request: TransferCreateRequest,
        capture_source: str = "manual",
        source_proposal_id: UUID | None = None,
    ) -> TransferRecord:
        with self.session_factory() as session, session.begin():
            repository = TransferRepository(session)
            if source_proposal_id is not None:
                existing = repository.find_by_source_proposal_id(
                    owner_id=owner_id, proposal_id=source_proposal_id
                )
                if existing is not None:
                    return existing
            destination_amount = self._validate_references(
                repository, owner_id=owner_id, request=request
            )
            transfer_id = repository.create(
                owner_id=owner_id,
                request=request,
                destination_amount=destination_amount,
                capture_source=capture_source,
                source_proposal_id=source_proposal_id,
            )
            record = repository.get(owner_id=owner_id, transfer_id=transfer_id)
            if record is None:
                raise RuntimeError("New transfer disappeared")
            return record

    @staticmethod
    def _validate_references(
        repository: TransferRepository, *, owner_id: int, request: TransferCreateRequest
    ) -> Decimal:
        if request.source_account_id == request.destination_account_id:
            raise TransferSameAccountError
        source = repository.owned_account(owner_id=owner_id, account_id=request.source_account_id)
        if source is None or not source.is_active:
            raise TransferAccountUnavailableError("source_account_id")
        destination = repository.owned_account(
            owner_id=owner_id, account_id=request.destination_account_id
        )
        if destination is None or not destination.is_active:
            raise TransferAccountUnavailableError("destination_account_id")
        source_code = repository.account_currency_code(account_id=source.id)
        destination_code = repository.account_currency_code(account_id=destination.id)
        if source_code == destination_code:
            if (
                request.destination_amount is not None
                and request.destination_amount != request.source_amount
            ):
                raise TransferSourceDestinationMismatchError
            return request.source_amount
        if request.destination_amount is None:
            raise TransferDestinationAmountRequiredError
        return request.destination_amount

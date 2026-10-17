from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from finance_ai.core.errors import (
    AccountNotFoundError,
    IncomeAccountUnavailableError,
    IncomeNotFoundError,
    IncomeSourceUnavailableError,
)
from finance_ai.incomes.repository import IncomeRepository
from finance_ai.incomes.schemas import IncomeRecord, IncomeWriteRequest


class IncomeService:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def create(
        self,
        *,
        owner_id: int,
        request: IncomeWriteRequest,
        capture_source: str = "manual",
        source_proposal_id: UUID | None = None,
    ) -> IncomeRecord:
        record, _ = self.create_with_replay(
            owner_id=owner_id,
            request=request,
            capture_source=capture_source,
            source_proposal_id=source_proposal_id,
        )
        return record

    def create_with_replay(
        self,
        *,
        owner_id: int,
        request: IncomeWriteRequest,
        capture_source: str,
        source_proposal_id: UUID | None,
    ) -> tuple[IncomeRecord, bool]:
        """Creates a text-capture income and reports whether it was a replay."""
        with self.session_factory() as session, session.begin():
            repository = IncomeRepository(session)
            if source_proposal_id is not None:
                existing = repository.find_by_source_proposal_id(
                    owner_id=owner_id, proposal_id=source_proposal_id
                )
                if existing is not None:
                    return existing, True
            self._validate_references(repository, owner_id=owner_id, request=request)
            income_id = repository.create(
                owner_id=owner_id,
                request=request,
                capture_source=capture_source,
                source_proposal_id=source_proposal_id,
            )
            record = repository.get(owner_id=owner_id, income_id=income_id)
            if record is None:
                raise RuntimeError("New income disappeared")
            return record, False

    def list(self, *, owner_id: int, account_id: int | None = None) -> list[IncomeRecord]:
        with self.session_factory() as session:
            repository = IncomeRepository(session)
            if (
                account_id is not None
                and repository.owned_account(owner_id=owner_id, account_id=account_id) is None
            ):
                raise AccountNotFoundError
            return repository.list(owner_id=owner_id, account_id=account_id)

    def get(self, *, owner_id: int, income_id: int) -> IncomeRecord:
        with self.session_factory() as session:
            record = IncomeRepository(session).get(owner_id=owner_id, income_id=income_id)
            if record is None:
                raise IncomeNotFoundError
            return record

    def update(self, *, owner_id: int, income_id: int, request: IncomeWriteRequest) -> IncomeRecord:
        with self.session_factory() as session, session.begin():
            repository = IncomeRepository(session)
            transaction = repository.owned_transaction(owner_id=owner_id, income_id=income_id)
            if transaction is None:
                raise IncomeNotFoundError
            self._validate_references(repository, owner_id=owner_id, request=request)
            repository.update(transaction=transaction, request=request)
            record = repository.get(owner_id=owner_id, income_id=income_id)
            if record is None:
                raise RuntimeError("Updated income disappeared")
            return record

    def delete(self, *, owner_id: int, income_id: int) -> None:
        with self.session_factory() as session, session.begin():
            repository = IncomeRepository(session)
            transaction = repository.owned_transaction(owner_id=owner_id, income_id=income_id)
            if transaction is None:
                raise IncomeNotFoundError
            repository.delete(transaction=transaction)

    @staticmethod
    def _validate_references(
        repository: IncomeRepository, *, owner_id: int, request: IncomeWriteRequest
    ) -> None:
        account = repository.owned_account(owner_id=owner_id, account_id=request.account_id)
        if account is None or not account.is_active:
            raise IncomeAccountUnavailableError
        if (
            request.income_source_id is not None
            and repository.owned_source(owner_id=owner_id, source_id=request.income_source_id)
            is None
        ):
            raise IncomeSourceUnavailableError

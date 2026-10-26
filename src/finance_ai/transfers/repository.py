"""PostgreSQL persistence for atomic account transfers."""

from datetime import UTC, datetime, time
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_ai.db.models.accounts import Account
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement, FinancialTransaction
from finance_ai.transfers.schemas import (
    TransferAccountSummary,
    TransferCreateRequest,
    TransferRecord,
)


class TransferRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def owned_account(self, *, owner_id: int, account_id: int) -> Account | None:
        return self.session.scalar(
            select(Account)
            .where(Account.user_id == owner_id, Account.id == account_id)
            .with_for_update(of=Account)
        )

    def account_currency_code(self, *, account_id: int) -> str | None:
        return self.session.scalar(
            select(Currency.code)
            .join(Account, Account.currency_id == Currency.id)
            .where(Account.id == account_id)
        )

    def create(
        self,
        *,
        owner_id: int,
        request: TransferCreateRequest,
        destination_amount: Decimal,
        capture_source: str = "manual",
        source_proposal_id: UUID | None = None,
    ) -> int:
        transaction = FinancialTransaction(
            user_id=owner_id,
            transaction_type="transfer",
            status="confirmed",
            occurred_at=datetime.combine(request.date, time.min, UTC),
            description=request.description,
            capture_source=capture_source,
            source_proposal_id=source_proposal_id,
        )
        self.session.add(transaction)
        self.session.flush()
        self.session.add(
            AccountMovement(
                user_id=owner_id,
                transaction_id=transaction.id,
                account_id=request.source_account_id,
                amount=-request.source_amount,
            )
        )
        self.session.add(
            AccountMovement(
                user_id=owner_id,
                transaction_id=transaction.id,
                account_id=request.destination_account_id,
                amount=destination_amount,
            )
        )
        self.session.flush()
        return transaction.id

    def get(self, *, owner_id: int, transfer_id: int) -> TransferRecord | None:
        transaction = self.session.scalar(
            select(FinancialTransaction).where(
                FinancialTransaction.user_id == owner_id,
                FinancialTransaction.id == transfer_id,
                FinancialTransaction.transaction_type == "transfer",
                FinancialTransaction.status == "confirmed",
            )
        )
        if transaction is None:
            return None
        movements = self.session.scalars(
            select(AccountMovement)
            .where(
                AccountMovement.user_id == owner_id,
                AccountMovement.transaction_id == transfer_id,
            )
            .order_by(AccountMovement.id)
        ).all()
        if len(movements) != 2:
            raise RuntimeError("Confirmed transfer must hold two movements")
        source_movement, destination_movement = movements
        return TransferRecord(
            id=transaction.id,
            source_account=self._account_summary(source_movement.account_id),
            source_amount=Decimal(-source_movement.amount),
            destination_account=self._account_summary(destination_movement.account_id),
            destination_amount=Decimal(destination_movement.amount),
            date=transaction.occurred_at.date(),
            description=transaction.description,
            created_at=transaction.created_at,
            updated_at=transaction.updated_at,
        )

    def find_by_source_proposal_id(
        self, *, owner_id: int, proposal_id: UUID
    ) -> TransferRecord | None:
        transfer_id = self.session.scalar(
            select(FinancialTransaction.id).where(
                FinancialTransaction.user_id == owner_id,
                FinancialTransaction.source_proposal_id == proposal_id,
                FinancialTransaction.transaction_type == "transfer",
            )
        )
        if transfer_id is None:
            return None
        return self.get(owner_id=owner_id, transfer_id=transfer_id)

    def _account_summary(self, account_id: int) -> TransferAccountSummary:
        row = self.session.execute(
            select(Account, Currency)
            .join(Currency, Currency.id == Account.currency_id)
            .where(Account.id == account_id)
        ).one()
        account, currency = row
        return TransferAccountSummary(id=account.id, name=account.name, currency_code=currency.code)

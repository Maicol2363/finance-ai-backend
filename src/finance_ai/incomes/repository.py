from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from finance_ai.db.models.accounts import Account
from finance_ai.db.models.catalogs import IncomeSource
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement, FinancialTransaction
from finance_ai.incomes.schemas import IncomeRecord, IncomeWriteRequest

IncomeRow = Row[
    tuple[FinancialTransaction, AccountMovement, Account, Currency, IncomeSource | None]
]


class IncomeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def owned_account(self, *, owner_id: int, account_id: int) -> Account | None:
        return self.session.scalar(
            select(Account)
            .where(Account.user_id == owner_id, Account.id == account_id)
            .with_for_update(of=Account)
        )

    def owned_source(self, *, owner_id: int, source_id: int) -> IncomeSource | None:
        return self.session.scalar(
            select(IncomeSource).where(
                IncomeSource.user_id == owner_id,
                IncomeSource.id == source_id,
                IncomeSource.is_active.is_(True),
            )
        )

    def owned_transaction(self, *, owner_id: int, income_id: int) -> FinancialTransaction | None:
        return self.session.scalar(
            select(FinancialTransaction)
            .where(
                FinancialTransaction.user_id == owner_id,
                FinancialTransaction.id == income_id,
                FinancialTransaction.transaction_type == "income",
                FinancialTransaction.status == "confirmed",
            )
            .with_for_update(of=FinancialTransaction)
        )

    def create(self, *, owner_id: int, request: IncomeWriteRequest) -> int:
        transaction = FinancialTransaction(
            user_id=owner_id,
            transaction_type="income",
            status="confirmed",
            occurred_at=datetime.combine(request.date, time.min, UTC),
            description=request.description,
            income_source_id=request.income_source_id,
            capture_source="manual",
        )
        self.session.add(transaction)
        self.session.flush()
        self.session.add(
            AccountMovement(
                user_id=owner_id,
                transaction_id=transaction.id,
                account_id=request.account_id,
                amount=request.amount,
            )
        )
        self.session.flush()
        return transaction.id

    def update(self, *, transaction: FinancialTransaction, request: IncomeWriteRequest) -> None:
        movement = self.session.scalar(
            select(AccountMovement)
            .where(
                AccountMovement.user_id == transaction.user_id,
                AccountMovement.transaction_id == transaction.id,
            )
            .with_for_update(of=AccountMovement)
        )
        if movement is None:
            raise RuntimeError("Confirmed income has no movement")
        transaction.occurred_at = datetime.combine(request.date, time.min, UTC)
        transaction.description = request.description
        transaction.income_source_id = request.income_source_id
        movement.account_id = request.account_id
        movement.amount = request.amount
        self.session.flush()

    def delete(self, *, transaction: FinancialTransaction) -> None:
        self.session.delete(transaction)
        self.session.flush()

    def get(self, *, owner_id: int, income_id: int) -> IncomeRecord | None:
        statement = self._projection(owner_id).where(FinancialTransaction.id == income_id)
        row = self.session.execute(statement).one_or_none()
        return self._record(row) if row is not None else None

    def list(self, *, owner_id: int, account_id: int | None = None) -> list[IncomeRecord]:
        statement = self._projection(owner_id)
        if account_id is not None:
            statement = statement.where(AccountMovement.account_id == account_id)
        rows = self.session.execute(
            statement.order_by(
                FinancialTransaction.occurred_at.desc(), FinancialTransaction.id.desc()
            )
        ).all()
        return [self._record(row) for row in rows]

    @staticmethod
    def _projection(owner_id: int):  # type: ignore[no-untyped-def]
        return (
            select(FinancialTransaction, AccountMovement, Account, Currency, IncomeSource)
            .join(
                AccountMovement,
                (AccountMovement.transaction_id == FinancialTransaction.id)
                & (AccountMovement.user_id == FinancialTransaction.user_id),
            )
            .join(
                Account,
                (Account.id == AccountMovement.account_id)
                & (Account.user_id == AccountMovement.user_id),
            )
            .join(Currency, Currency.id == Account.currency_id)
            .outerjoin(IncomeSource, IncomeSource.id == FinancialTransaction.income_source_id)
            .where(
                FinancialTransaction.user_id == owner_id,
                FinancialTransaction.transaction_type == "income",
                FinancialTransaction.status == "confirmed",
            )
        )

    @staticmethod
    def _record(row: IncomeRow) -> IncomeRecord:
        transaction, movement, account, currency, source = row
        return IncomeRecord(
            id=transaction.id,
            account_id=account.id,
            account_name=account.name,
            currency_code=currency.code,
            amount=Decimal(movement.amount),
            date=transaction.occurred_at.date(),
            description=transaction.description,
            income_source_id=transaction.income_source_id,
            income_source_name=source.name if source is not None else None,
            created_at=transaction.created_at,
            updated_at=transaction.updated_at,
        )

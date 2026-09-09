from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from finance_ai.accounts.repositories import (
    AccountMovementConstraintError,
    AccountRecord,
    ActiveAccountNameConstraintError,
)
from finance_ai.accounts.schemas import AccountType, CurrencySummary
from finance_ai.db.models.accounts import Account
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement, FinancialTransaction

ACTIVE_NAME_CONSTRAINT = "uq_account_active_name"
ACCOUNT_MOVEMENT_CONSTRAINT = "fk_account_movement_account_owner"


def is_active_name_conflict(exc: BaseException) -> bool:
    if not isinstance(exc, IntegrityError):
        return False
    diagnostic = getattr(exc.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == ACTIVE_NAME_CONSTRAINT


def is_account_movement_constraint(exc: BaseException) -> bool:
    if not isinstance(exc, IntegrityError):
        return False
    diagnostic = getattr(exc.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == ACCOUNT_MOVEMENT_CONSTRAINT


class SQLAlchemyAccountRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_owned(self, *, owner_id: int, include_inactive: bool) -> list[AccountRecord]:
        statement = self._account_projection(owner_id).where(Account.user_id == owner_id)
        if not include_inactive:
            statement = statement.where(Account.is_active.is_(True))
        rows = self.session.execute(
            statement.order_by(
                Account.is_active.desc(),
                func.lower(Account.name),
                Account.id,
            )
        ).all()
        return [
            self._record(account, currency, balance, confirmed_count)
            for account, currency, balance, confirmed_count in rows
        ]

    def find_owned(
        self, *, owner_id: int, account_id: int, for_update: bool = False
    ) -> AccountRecord | None:
        statement = self._account_projection(owner_id).where(
            Account.user_id == owner_id,
            Account.id == account_id,
        )
        if for_update:
            statement = statement.with_for_update(of=Account)
        row = self.session.execute(statement).one_or_none()
        if row is None:
            return None
        account, currency, balance, confirmed_count = row
        return self._record(account, currency, balance, confirmed_count)

    def find_active_owned(self, *, owner_id: int, account_id: int) -> AccountRecord | None:
        statement = self._account_projection(owner_id).where(
            Account.user_id == owner_id,
            Account.id == account_id,
            Account.is_active.is_(True),
        )
        row = self.session.execute(statement).one_or_none()
        if row is None:
            return None
        account, currency, balance, confirmed_count = row
        return self._record(account, currency, balance, confirmed_count)

    def active_name_exists(
        self, *, owner_id: int, normalized_name: str, excluding_id: int | None = None
    ) -> bool:
        statement = select(Account.id).where(
            Account.user_id == owner_id,
            Account.is_active.is_(True),
            func.lower(Account.name) == normalized_name.lower(),
        )
        if excluding_id is not None:
            statement = statement.where(Account.id != excluding_id)
        return self.session.scalar(statement.limit(1)) is not None

    def add(
        self,
        *,
        owner_id: int,
        name: str,
        account_type: AccountType,
        currency_id: int,
        opening_balance: Decimal,
    ) -> int:
        account = Account(
            user_id=owner_id,
            name=name,
            account_type=account_type.value,
            currency_id=currency_id,
            opening_balance=opening_balance,
            is_active=True,
        )
        self.session.add(account)
        try:
            self.session.flush()
        except IntegrityError as exc:
            if is_active_name_conflict(exc):
                raise ActiveAccountNameConstraintError from None
            raise
        return account.id

    def update_metadata(
        self,
        *,
        owner_id: int,
        account_id: int,
        name: str | None,
        account_type: AccountType | None,
        currency_id: int | None,
        opening_balance: Decimal | None,
    ) -> None:
        values: dict[str, object] = {"updated_at": func.now()}
        if name is not None:
            values["name"] = name
        if account_type is not None:
            values["account_type"] = account_type.value
        if currency_id is not None:
            values["currency_id"] = currency_id
        if opening_balance is not None:
            values["opening_balance"] = opening_balance
        try:
            self.session.execute(
                update(Account)
                .where(Account.user_id == owner_id, Account.id == account_id)
                .values(**values)
            )
        except IntegrityError as exc:
            if is_active_name_conflict(exc):
                raise ActiveAccountNameConstraintError from None
            raise

    def set_active(self, *, owner_id: int, account_id: int, is_active: bool) -> None:
        try:
            self.session.execute(
                update(Account)
                .where(Account.user_id == owner_id, Account.id == account_id)
                .values(is_active=is_active, updated_at=func.now())
            )
        except IntegrityError as exc:
            if is_active_name_conflict(exc):
                raise ActiveAccountNameConstraintError from None
            raise

    def has_confirmed_movements(self, *, owner_id: int, account_id: int) -> bool:
        return (
            self.session.scalar(
                select(AccountMovement.id)
                .join(
                    FinancialTransaction,
                    and_(
                        FinancialTransaction.id == AccountMovement.transaction_id,
                        FinancialTransaction.user_id == AccountMovement.user_id,
                    ),
                )
                .where(
                    AccountMovement.user_id == owner_id,
                    AccountMovement.account_id == account_id,
                    FinancialTransaction.status == "confirmed",
                )
                .limit(1)
            )
            is not None
        )

    def has_movements(self, *, owner_id: int, account_id: int) -> bool:
        return (
            self.session.scalar(
                select(AccountMovement.id)
                .where(
                    AccountMovement.user_id == owner_id,
                    AccountMovement.account_id == account_id,
                )
                .limit(1)
            )
            is not None
        )

    def delete(self, *, owner_id: int, account_id: int) -> None:
        try:
            self.session.execute(
                delete(Account).where(Account.user_id == owner_id, Account.id == account_id)
            )
        except IntegrityError as exc:
            if is_account_movement_constraint(exc):
                raise AccountMovementConstraintError from None
            raise

    def latest_active_currency_code(self, *, owner_id: int) -> str | None:
        return self.session.scalar(
            select(Currency.code)
            .join(Account, Account.currency_id == Currency.id)
            .where(Account.user_id == owner_id, Currency.is_active.is_(True))
            .order_by(Account.created_at.desc(), Account.id.desc())
            .limit(1)
        )

    @staticmethod
    def _account_projection(
        owner_id: int,
    ) -> Select[tuple[Account, Currency, Decimal, int]]:
        totals = (
            select(
                AccountMovement.account_id.label("account_id"),
                AccountMovement.user_id.label("user_id"),
                func.sum(AccountMovement.amount).label("movement_total"),
                func.count(AccountMovement.id).label("confirmed_count"),
            )
            .join(
                FinancialTransaction,
                and_(
                    FinancialTransaction.id == AccountMovement.transaction_id,
                    FinancialTransaction.user_id == AccountMovement.user_id,
                ),
            )
            .where(
                AccountMovement.user_id == owner_id,
                FinancialTransaction.status == "confirmed",
            )
            .group_by(AccountMovement.account_id, AccountMovement.user_id)
            .subquery()
        )
        balance = (
            Account.opening_balance + func.coalesce(totals.c.movement_total, Decimal("0.00"))
        ).label("balance")
        confirmed_count = func.coalesce(totals.c.confirmed_count, 0).label("confirmed_count")
        return (
            select(Account, Currency, balance, confirmed_count)
            .join(Currency, Currency.id == Account.currency_id)
            .outerjoin(
                totals,
                and_(
                    totals.c.account_id == Account.id,
                    totals.c.user_id == Account.user_id,
                ),
            )
        )

    @staticmethod
    def _record(
        account: Account,
        currency: Currency,
        balance: Any,
        confirmed_count: int,
    ) -> AccountRecord:
        return AccountRecord(
            id=account.id,
            user_id=account.user_id,
            name=account.name,
            account_type=AccountType(account.account_type),
            currency=CurrencySummary(
                id=currency.id,
                code=currency.code,
                name=currency.name,
                decimals=currency.decimals,
            ),
            opening_balance=account.opening_balance,
            balance=Decimal(balance),
            currency_is_locked=confirmed_count > 0,
            is_active=account.is_active,
            created_at=account.created_at,
            updated_at=account.updated_at,
        )


class SQLAlchemyAccountCurrencyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_active(self) -> list[CurrencySummary]:
        currencies = self.session.scalars(
            select(Currency).where(Currency.is_active.is_(True)).order_by(Currency.code)
        )
        return [self._summary(currency) for currency in currencies]

    def find_active_by_code(self, code: str) -> CurrencySummary | None:
        currency = self.session.scalar(
            select(Currency).where(Currency.code == code, Currency.is_active.is_(True)).limit(1)
        )
        return None if currency is None else self._summary(currency)

    def first_active_code(self, codes: tuple[str, ...]) -> str | None:
        if not codes:
            return None
        active_codes = set(
            self.session.scalars(
                select(Currency.code).where(Currency.code.in_(codes), Currency.is_active.is_(True))
            )
        )
        return next((code for code in codes if code in active_codes), None)

    @staticmethod
    def _summary(currency: Currency) -> CurrencySummary:
        return CurrencySummary(
            id=currency.id,
            code=currency.code,
            name=currency.name,
            decimals=currency.decimals,
        )

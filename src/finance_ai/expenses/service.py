"""Business rules for expense correction and unified history queries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from finance_ai.core.errors import (
    ExpenseAccountInvalidError,
    ExpenseCategoryNotFoundError,
    ExpenseNotEditableError,
    ExpenseNotFoundError,
    HistoryFilterInvalidError,
    RequestInvalidError,
)
from finance_ai.expenses.repositories import ExpenseRecord, HistoryFilters
from finance_ai.expenses.schemas import (
    ExpenseListResult,
    ExpenseUpdateCommand,
    HistoryPeriod,
    HistoryResult,
)
from finance_ai.expenses.uow import ExpenseUnitOfWork

ExpenseUnitOfWorkFactory = Callable[[], ExpenseUnitOfWork]
# receipt.total and expense.amount are NUMERIC(12, 2) in the persisted schema.
MONEY_MAX = Decimal("9999999999.99")


class ExpenseService:
    """Coordinates HU-008 mutations and HU-011 history reads in one UoW."""

    def __init__(self, *, uow_factory: ExpenseUnitOfWorkFactory) -> None:
        self.uow_factory = uow_factory

    def list(self, *, owner_id: int, page: int, page_size: int) -> ExpenseListResult:
        if page < 1 or page_size < 1 or page_size > 100:
            raise HistoryFilterInvalidError(
                {"page": ["Page must be positive."]}
                if page < 1
                else {"page_size": ["Page size must be between 1 and 100."]}
            )
        with self.uow_factory() as uow:
            items, total = uow.expenses.list_owned(
                owner_id=owner_id,
                offset=(page - 1) * page_size,
                limit=page_size,
            )
        return ExpenseListResult(items=tuple(items), total=total, page=page, page_size=page_size)

    def get(self, *, owner_id: int, expense_id: int) -> ExpenseRecord:
        with self.uow_factory() as uow:
            expense = uow.expenses.find_owned(owner_id=owner_id, expense_id=expense_id)
        if expense is None:
            raise ExpenseNotFoundError
        return expense

    def update(
        self,
        *,
        owner_id: int,
        expense_id: int,
        command: ExpenseUpdateCommand,
    ) -> ExpenseRecord:
        self._validate_command(command)
        with self.uow_factory() as uow:
            current = uow.expenses.find_owned(
                owner_id=owner_id,
                expense_id=expense_id,
                for_update=True,
            )
            current = self._require_editable(current)

            if (
                "category_id" in command.fields
                and command.category_id is not None
                and not uow.expenses.category_owned(
                    owner_id=owner_id, category_id=command.category_id
                )
            ):
                raise ExpenseCategoryNotFoundError

            account_id = current.account_id
            if "account_id" in command.fields:
                assert command.account_id is not None
                account_id = command.account_id
            if "account_id" in command.fields or "amount" in command.fields:
                currency = uow.expenses.active_account_currency(
                    owner_id=owner_id, account_id=account_id
                )
                if currency is None or currency.code != current.currency.code:
                    raise ExpenseAccountInvalidError

            uow.expenses.update(
                owner_id=owner_id,
                expense_id=expense_id,
                fields=command.fields,
                name=command.name,
                amount=command.amount,
                note=command.note,
                receipt_name=command.receipt_name,
                receipt_note=command.receipt_note,
                category_id=command.category_id,
                account_id=account_id if "account_id" in command.fields else None,
                occurred_at=command.occurred_at,
                description=command.description,
            )
            uow.flush()
            updated = uow.expenses.find_owned(owner_id=owner_id, expense_id=expense_id)
            if updated is None:
                raise ExpenseNotFoundError
            uow.commit()
            return updated

    def delete(self, *, owner_id: int, expense_id: int) -> None:
        with self.uow_factory() as uow:
            current = uow.expenses.find_owned(
                owner_id=owner_id,
                expense_id=expense_id,
                for_update=True,
            )
            self._require_editable(current)
            uow.expenses.delete(owner_id=owner_id, expense_id=expense_id)
            uow.flush()
            uow.commit()

    def history(
        self,
        *,
        owner_id: int,
        search: str | None,
        transaction_type: str | None,
        category_id: int | None,
        account_id: int | None,
        period: HistoryPeriod | None,
        start_date: date | None,
        end_date: date | None,
        page: int,
        page_size: int,
    ) -> HistoryResult:
        if page < 1 or page_size < 1 or page_size > 100:
            raise HistoryFilterInvalidError(
                {"page": ["Page must be positive."]}
                if page < 1
                else {"page_size": ["Page size must be between 1 and 100."]}
            )
        if category_id is not None and category_id < 1:
            raise HistoryFilterInvalidError({"category_id": ["Category id must be positive."]})
        if account_id is not None and account_id < 1:
            raise HistoryFilterInvalidError({"account_id": ["Account id must be positive."]})
        start_date, end_date = self._period_dates(
            period=period,
            start_date=start_date,
            end_date=end_date,
        )
        with self.uow_factory() as uow:
            items, total = uow.expenses.list_history(
                owner_id=owner_id,
                filters=HistoryFilters(
                    search=None if search is None or not search.strip() else search.strip(),
                    transaction_type=transaction_type,
                    category_id=category_id,
                    account_id=account_id,
                    start_date=start_date,
                    end_date=end_date,
                    offset=(page - 1) * page_size,
                    limit=page_size,
                ),
            )
        return HistoryResult(items=tuple(items), total=total, page=page, page_size=page_size)

    @staticmethod
    def _require_editable(expense: ExpenseRecord | None) -> ExpenseRecord:
        if expense is None:
            raise ExpenseNotFoundError
        if expense.status != "confirmed":
            raise ExpenseNotEditableError
        return expense

    @staticmethod
    def _validate_command(command: ExpenseUpdateCommand) -> None:
        required_values = {
            "name": command.name,
            "amount": command.amount,
            "account_id": command.account_id,
            "occurred_at": command.occurred_at,
            "receipt_name": command.receipt_name,
        }
        invalid_nulls = [
            field
            for field in command.fields
            if field in required_values and required_values[field] is None
        ]
        if invalid_nulls:
            raise RequestInvalidError(
                {field: ["This field cannot be null."] for field in invalid_nulls}
            )
        if command.name is not None and not command.name.strip():
            raise RequestInvalidError({"name": ["Use a non-empty name."]})
        if command.receipt_name is not None and not command.receipt_name.strip():
            raise RequestInvalidError({"receipt_name": ["Use a non-empty merchant name."]})
        if command.occurred_at is not None and (
            command.occurred_at.tzinfo is None or command.occurred_at.utcoffset() is None
        ):
            raise RequestInvalidError({"occurred_at": ["Include a timezone in the date."]})
        if command.amount is not None:
            try:
                quantized = command.amount.quantize(Decimal("0.01"))
            except InvalidOperation as exc:
                raise RequestInvalidError({"amount": ["Use at most two decimal places."]}) from exc
            if (
                quantized != command.amount
                or not command.amount.is_finite()
                or command.amount <= 0
                or command.amount > MONEY_MAX
            ):
                raise RequestInvalidError({"amount": ["Use a valid positive amount."]})

    @staticmethod
    def _period_dates(
        *,
        period: HistoryPeriod | None,
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[date | None, date | None]:
        if period is None:
            if start_date is not None or end_date is not None:
                raise HistoryFilterInvalidError(
                    {"period": ["Use period=custom with start_date and end_date."]}
                )
            return None, None
        if period is HistoryPeriod.CUSTOM:
            if start_date is None or end_date is None or end_date < start_date:
                raise HistoryFilterInvalidError(
                    {"start_date": ["Custom periods require an ordered date range."]}
                )
            return start_date, end_date
        if start_date is not None or end_date is not None:
            raise HistoryFilterInvalidError(
                {"period": ["Do not send dates with weekly or monthly periods."]}
            )
        today = datetime.now(UTC).date()
        if period in (HistoryPeriod.WEEK, HistoryPeriod.WEEKLY):
            return today - timedelta(days=today.weekday()), today
        return today.replace(day=1), today

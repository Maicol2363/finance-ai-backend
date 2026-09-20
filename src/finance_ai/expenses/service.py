"""Business rules for expense correction and unified history queries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

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
    CreateExpenseCommand,
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
    """Coordinates HU-006 creation, HU-008 mutations, and HU-011 history reads in one UoW."""

    def __init__(self, *, uow_factory: ExpenseUnitOfWorkFactory) -> None:
        self.uow_factory = uow_factory

    def create(
        self,
        *,
        owner_id: int,
        command: CreateExpenseCommand,
    ) -> ExpenseRecord:
        if command.amount <= Decimal("0") or command.amount > MONEY_MAX:
            raise RequestInvalidError(
                {"amount": [f"Amount must be between 0.01 and {MONEY_MAX}."]}
            )
        if not command.merchant.strip():
            raise RequestInvalidError(
                {"merchant": ["Merchant cannot be empty."]}
            )

        with self.uow_factory() as uow:
            account_currency = uow.expenses.active_account_currency(
                owner_id=owner_id, account_id=command.account_id
            )
            if account_currency is None:
                raise ExpenseAccountInvalidError

            if (
                command.category_id is not None
                and not uow.expenses.category_owned(
                    owner_id=owner_id, category_id=command.category_id
                )
            ):
                raise ExpenseCategoryNotFoundError

            created = uow.expenses.create_manual(
                owner_id=owner_id,
                account_id=command.account_id,
                amount=command.amount,
                merchant=command.merchant.strip(),
                occurred_at=command.occurred_at,
                category_id=command.category_id,
                note=command.note,
            )
            uow.flush()
            uow.commit()
            return created

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
        if transaction_type is not None and transaction_type not in {
            "expense",
            "income",
            "transfer",
        }:
            raise HistoryFilterInvalidError(
                {"transaction_type": ["Transaction type is unsupported."]}
            )

        resolved_start, resolved_end = self._resolve_period_dates(
            period=period,
            start_date=start_date,
            end_date=end_date,
        )
        filters = HistoryFilters(
            search=search,
            transaction_type=transaction_type,
            category_id=category_id,
            account_id=account_id,
            start_date=resolved_start,
            end_date=resolved_end,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        with self.uow_factory() as uow:
            items, total = uow.expenses.list_history(owner_id=owner_id, filters=filters)
        return HistoryResult(items=tuple(items), total=total, page=page, page_size=page_size)

    def _require_editable(self, expense: ExpenseRecord | None) -> ExpenseRecord:
        if expense is None:
            raise ExpenseNotFoundError
        if expense.status != "confirmed":
            raise ExpenseNotEditableError
        return expense

    def _validate_command(self, command: ExpenseUpdateCommand) -> None:
        errors: dict[str, list[str]] = {}
        if "name" in command.fields and (command.name is None or not command.name.strip()):
            errors["name"] = ["Expense name cannot be empty."]
        if "amount" in command.fields:
            if command.amount is None or command.amount <= Decimal("0"):
                errors["amount"] = ["Amount must be greater than zero."]
            elif command.amount > MONEY_MAX:
                errors["amount"] = [f"Amount cannot exceed {MONEY_MAX}."]
        if "receipt_name" in command.fields and (
            command.receipt_name is None or not command.receipt_name.strip()
        ):
            errors["receipt_name"] = ["Receipt name cannot be empty."]
        if "account_id" in command.fields and (
            command.account_id is None or command.account_id < 1
        ):
            errors["account_id"] = ["Account id must be positive."]
        if "category_id" in command.fields and (
            command.category_id is not None and command.category_id < 1
        ):
            errors["category_id"] = ["Category id must be positive."]
        if errors:
            raise RequestInvalidError(errors)

    def _resolve_period_dates(
        self,
        *,
        period: HistoryPeriod | None,
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[date | None, date | None]:
        if period is None and start_date is None and end_date is None:
            return None, None
        if start_date is not None and end_date is not None and start_date > end_date:
            raise HistoryFilterInvalidError(
                {"date_range": ["Start date must be on or before end date."]}
            )
        if period in (HistoryPeriod.CUSTOM, None):
            return start_date, end_date

        today = datetime.now(UTC).date()
        if period in (HistoryPeriod.WEEK, HistoryPeriod.WEEKLY):
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
            return start, end
        if period in (HistoryPeriod.MONTH, HistoryPeriod.MONTHLY):
            start = today.replace(day=1)
            # Find last day of month
            if today.month == 12:
                next_month = today.replace(year=today.year + 1, month=1, day=1)
            else:
                next_month = today.replace(month=today.month + 1, day=1)
            end = next_month - timedelta(days=1)
            return start, end
        return start_date, end_date

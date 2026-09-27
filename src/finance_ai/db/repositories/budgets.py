from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from finance_ai.accounts.schemas import CurrencySummary
from finance_ai.budgets.periods import FrequencyUnit, ScheduleType
from finance_ai.budgets.repositories import (
    BudgetCategoryRecord,
    BudgetRecord,
    DailySpendingRecord,
)
from finance_ai.db.models.budgets import Budget
from finance_ai.db.models.catalogs import Category
from finance_ai.db.models.receipts import Expense, Receipt
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import FinancialTransaction


class SQLAlchemyBudgetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_owned(self, *, owner_id: int) -> list[BudgetRecord]:
        rows = self.session.execute(
            self._projection(owner_id)
            .where(Budget.is_active.is_(True))
            .order_by(Budget.created_at.desc(), Budget.id.desc())
        ).all()
        return [self._record(*row) for row in rows]

    def find_owned(
        self, *, owner_id: int, budget_id: int, for_update: bool = False
    ) -> BudgetRecord | None:
        statement = self._projection(owner_id).where(Budget.id == budget_id)
        if for_update:
            statement = statement.with_for_update(of=Budget)
        row = self.session.execute(statement).first()
        return None if row is None else self._record(*row)

    def list_categories(self, *, owner_id: int) -> list[BudgetCategoryRecord]:
        categories = self.session.scalars(
            select(Category)
            .where(Category.user_id == owner_id)
            .order_by(Category.sort_order, Category.id)
        )
        return [self._category(item) for item in categories]

    def find_category_owned(
        self, *, owner_id: int, category_id: int
    ) -> BudgetCategoryRecord | None:
        category = self.session.scalar(
            select(Category).where(Category.id == category_id, Category.user_id == owner_id)
        )
        return None if category is None else self._category(category)

    def add(
        self,
        *,
        owner_id: int,
        category_id: int,
        currency_id: int,
        name: str,
        amount: Decimal,
        schedule_type: ScheduleType,
        frequency_unit: FrequencyUnit,
        frequency_interval: int,
        starts_on: date,
    ) -> int:
        item = Budget(
            user_id=owner_id,
            category_id=category_id,
            currency_id=currency_id,
            name=name,
            amount=amount,
            schedule_type=schedule_type.value,
            frequency_unit=frequency_unit.value,
            frequency_interval=frequency_interval,
            starts_on=starts_on,
            ends_on=None,
            is_active=True,
        )
        self.session.add(item)
        self.session.flush()
        return item.id

    def update(
        self,
        *,
        owner_id: int,
        budget_id: int,
        category_id: int | None,
        currency_id: int | None,
        name: str | None,
        amount: Decimal | None,
        frequency_unit: FrequencyUnit | None,
        starts_on: date | None,
    ) -> None:
        values: dict[str, object] = {"updated_at": func.now()}
        if category_id is not None:
            values["category_id"] = category_id
        if currency_id is not None:
            values["currency_id"] = currency_id
        if name is not None:
            values["name"] = name
        if amount is not None:
            values["amount"] = amount
        if frequency_unit is not None:
            values.update(
                schedule_type=ScheduleType.RECURRING.value,
                frequency_unit=frequency_unit.value,
                frequency_interval=1,
                starts_on=starts_on,
                ends_on=None,
            )
        self.session.execute(
            update(Budget)
            .where(Budget.id == budget_id, Budget.user_id == owner_id)
            .values(**values)
        )

    def delete(self, *, owner_id: int, budget_id: int) -> None:
        self.session.execute(
            delete(Budget).where(Budget.id == budget_id, Budget.user_id == owner_id)
        )

    def daily_spending(
        self, *, owner_id: int, starts_on: date, ends_on: date
    ) -> list[DailySpendingRecord]:
        effective_category = func.coalesce(Expense.category_id, Receipt.category_id)
        rows = self.session.execute(
            select(
                effective_category.label("category_id"),
                Receipt.currency_id,
                Receipt.date,
                func.coalesce(func.sum(Expense.amount), 0).label("amount"),
            )
            .join(Receipt, Receipt.id == Expense.receipt_id)
            .join(
                FinancialTransaction,
                FinancialTransaction.id == Receipt.transaction_id,
            )
            .where(
                Expense.user_id == owner_id,
                Receipt.user_id == owner_id,
                FinancialTransaction.user_id == owner_id,
                FinancialTransaction.transaction_type == "expense",
                FinancialTransaction.status == "confirmed",
                Receipt.date >= starts_on,
                Receipt.date <= ends_on,
            )
            .group_by(effective_category, Receipt.currency_id, Receipt.date)
        ).all()
        return [
            DailySpendingRecord(
                category_id=category_id,
                currency_id=currency_id,
                spent_on=spent_on,
                amount=Decimal(amount),
            )
            for category_id, currency_id, spent_on, amount in rows
        ]

    @staticmethod
    def _projection(owner_id: int):  # type: ignore[no-untyped-def]
        return (
            select(Budget, Currency, Category)
            .join(Currency, Currency.id == Budget.currency_id)
            .outerjoin(
                Category,
                (Category.id == Budget.category_id) & (Category.user_id == Budget.user_id),
            )
            .where(Budget.user_id == owner_id)
        )

    @classmethod
    def _record(cls, budget: Budget, currency: Currency, category: Category | None) -> BudgetRecord:
        return BudgetRecord(
            id=budget.id,
            user_id=budget.user_id,
            currency=CurrencySummary(
                id=currency.id,
                code=currency.code,
                name=currency.name,
                decimals=currency.decimals,
            ),
            category=None if category is None else cls._category(category),
            name=budget.name,
            amount=budget.amount,
            schedule_type=ScheduleType(budget.schedule_type),
            frequency_unit=(
                None if budget.frequency_unit is None else FrequencyUnit(budget.frequency_unit)
            ),
            frequency_interval=budget.frequency_interval,
            starts_on=budget.starts_on,
            ends_on=budget.ends_on,
            is_active=budget.is_active,
            created_at=budget.created_at,
            updated_at=budget.updated_at,
        )

    @staticmethod
    def _category(category: Category) -> BudgetCategoryRecord:
        return BudgetCategoryRecord(
            id=category.id,
            user_id=category.user_id,
            name=category.name,
            color=category.color,
            icon=category.icon,
        )

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from finance_ai.accounts.schemas import CurrencySummary
from finance_ai.budgets.periods import FrequencyUnit, ScheduleType


@dataclass(frozen=True)
class BudgetCategoryRecord:
    id: int
    user_id: int
    name: str
    color: str
    icon: str


@dataclass(frozen=True)
class BudgetRecord:
    id: int
    user_id: int
    currency: CurrencySummary
    category: BudgetCategoryRecord | None
    name: str
    amount: Decimal
    schedule_type: ScheduleType
    frequency_unit: FrequencyUnit | None
    frequency_interval: int | None
    starts_on: date
    ends_on: date | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DailySpendingRecord:
    category_id: int | None
    currency_id: int
    spent_on: date
    amount: Decimal


class BudgetRepository(Protocol):
    def list_owned(self, *, owner_id: int) -> list[BudgetRecord]: ...

    def find_owned(
        self, *, owner_id: int, budget_id: int, for_update: bool = False
    ) -> BudgetRecord | None: ...

    def list_categories(self, *, owner_id: int) -> list[BudgetCategoryRecord]: ...

    def find_category_owned(
        self, *, owner_id: int, category_id: int
    ) -> BudgetCategoryRecord | None: ...

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
    ) -> int: ...

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
    ) -> None: ...

    def delete(self, *, owner_id: int, budget_id: int) -> None: ...

    def daily_spending(
        self, *, owner_id: int, starts_on: date, ends_on: date
    ) -> list[DailySpendingRecord]: ...

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finance_ai.accounts.schemas import CurrencySummary
from finance_ai.budgets.periods import BudgetPeriod, FrequencyUnit, ScheduleType
from finance_ai.budgets.policy import BudgetPolicy
from finance_ai.budgets.repositories import (
    BudgetCategoryRecord,
    BudgetRecord,
    DailySpendingRecord,
)
from finance_ai.budgets.schemas import (
    BudgetCreateCommand,
    BudgetScheduleCommand,
    BudgetUpdateCommand,
)
from finance_ai.budgets.service import BudgetService
from finance_ai.core.errors import (
    BudgetCategoryUnavailableError,
    BudgetCurrencyInvalidError,
    BudgetNotFoundError,
)

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
TODAY = NOW.date()
COP = CurrencySummary(1, "COP", "Colombian Peso", 2)
USD = CurrencySummary(2, "USD", "US Dollar", 2)


@dataclass
class Store:
    categories: dict[int, BudgetCategoryRecord] = field(
        default_factory=lambda: {
            10: BudgetCategoryRecord(10, 7, "Food", "orange", "food"),
            11: BudgetCategoryRecord(11, 7, "Transport", "blue", "transport"),
            20: BudgetCategoryRecord(20, 8, "Private", "gray", "other"),
        }
    )
    currencies: dict[str, CurrencySummary] = field(default_factory=lambda: {"COP": COP, "USD": USD})
    budgets: dict[int, BudgetRecord] = field(default_factory=dict)
    spending: list[DailySpendingRecord] = field(default_factory=list)
    next_id: int = 1
    commits: int = 0


class FakeBudgets:
    def __init__(self, store: Store) -> None:
        self.store = store

    def list_owned(self, *, owner_id: int) -> list[BudgetRecord]:
        return [item for item in self.store.budgets.values() if item.user_id == owner_id]

    def find_owned(
        self, *, owner_id: int, budget_id: int, for_update: bool = False
    ) -> BudgetRecord | None:
        del for_update
        item = self.store.budgets.get(budget_id)
        return item if item is not None and item.user_id == owner_id else None

    def list_categories(self, *, owner_id: int) -> list[BudgetCategoryRecord]:
        return [item for item in self.store.categories.values() if item.user_id == owner_id]

    def find_category_owned(
        self, *, owner_id: int, category_id: int
    ) -> BudgetCategoryRecord | None:
        item = self.store.categories.get(category_id)
        return item if item is not None and item.user_id == owner_id else None

    def add(self, **values: object) -> int:
        item_id = self.store.next_id
        self.store.next_id += 1
        currency = next(
            item for item in self.store.currencies.values() if item.id == values["currency_id"]
        )
        category_id = values["category_id"]
        category = None if category_id is None else self.store.categories[int(category_id)]
        self.store.budgets[item_id] = BudgetRecord(
            id=item_id,
            user_id=int(values["owner_id"]),
            currency=currency,
            category=category,
            name=str(values["name"]),
            amount=values["amount"],  # type: ignore[arg-type]
            schedule_type=values["schedule_type"],  # type: ignore[arg-type]
            frequency_unit=values["frequency_unit"],  # type: ignore[arg-type]
            frequency_interval=values["frequency_interval"],  # type: ignore[arg-type]
            starts_on=values["starts_on"],  # type: ignore[arg-type]
            ends_on=values["ends_on"],  # type: ignore[arg-type]
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        return item_id

    def update(self, **values: object) -> None:
        budget_id = int(values["budget_id"])
        current = self.store.budgets[budget_id]
        category_id = values["category_id"]
        currency_id = values["currency_id"]
        category = current.category
        if values["category_changed"]:
            category = None if category_id is None else self.store.categories[int(category_id)]
        currency = current.currency
        if currency_id is not None:
            currency = next(
                item for item in self.store.currencies.values() if item.id == currency_id
            )
        self.store.budgets[budget_id] = replace(
            current,
            category=category,
            currency=currency,
            name=current.name if values["name"] is None else str(values["name"]),
            amount=current.amount if values["amount"] is None else values["amount"],  # type: ignore[arg-type]
            schedule_type=(
                current.schedule_type
                if values["schedule_type"] is None
                else values["schedule_type"]
            ),  # type: ignore[arg-type]
            frequency_unit=(
                current.frequency_unit
                if values["schedule_type"] is None
                else values["frequency_unit"]
            ),  # type: ignore[arg-type]
            frequency_interval=(
                current.frequency_interval
                if values["schedule_type"] is None
                else values["frequency_interval"]
            ),  # type: ignore[arg-type]
            starts_on=(current.starts_on if values["starts_on"] is None else values["starts_on"]),  # type: ignore[arg-type]
            ends_on=(current.ends_on if values["schedule_type"] is None else values["ends_on"]),  # type: ignore[arg-type]
            updated_at=NOW,
        )

    def delete(self, *, owner_id: int, budget_id: int) -> None:
        assert self.store.budgets[budget_id].user_id == owner_id
        del self.store.budgets[budget_id]

    def daily_spending(
        self, *, owner_id: int, starts_on: date, ends_on: date
    ) -> list[DailySpendingRecord]:
        del owner_id
        return [item for item in self.store.spending if starts_on <= item.spent_on <= ends_on]


class FakeCurrencies:
    def __init__(self, store: Store) -> None:
        self.store = store

    def list_active(self) -> list[CurrencySummary]:
        return list(self.store.currencies.values())

    def find_active_by_code(self, code: str) -> CurrencySummary | None:
        return self.store.currencies.get(code)

    def first_active_code(self, codes: tuple[str, ...]) -> str | None:
        return next((item for item in codes if item in self.store.currencies), None)


class FakeUow:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.budgets = FakeBudgets(store)
        self.currencies = FakeCurrencies(store)

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.store.commits += 1

    def rollback(self) -> None:
        return None


def make_service() -> tuple[BudgetService, Store]:
    store = Store()
    return (
        BudgetService(
            policy=BudgetPolicy(),
            uow_factory=lambda: FakeUow(store),  # type: ignore[arg-type]
            today_factory=lambda: TODAY,
        ),
        store,
    )


def create_budget(
    service: BudgetService,
    *,
    owner_id: int = 7,
    category_id: int = 10,
    currency: str = "COP",
    period: BudgetPeriod = BudgetPeriod.WEEKLY,
    amount: str = "100.00",
):
    return service.create(
        owner_id=owner_id,
        command=BudgetCreateCommand(
            category_id=category_id,
            amount=Decimal(amount),
            currency_code=currency,
            period=period,
        ),
    )


def test_create_anchors_simple_period_and_commits_owned_budget() -> None:
    service, store = make_service()

    result = create_budget(service, currency="cop")

    assert result.category is not None and result.category.name == "Food"
    assert result.frequency_unit is FrequencyUnit.WEEK
    assert result.starts_on == result.current_period_start == date(2026, 9, 21)
    assert result.current_period_end == date(2026, 9, 27)
    assert result.currency.code == "COP"
    assert store.commits == 1


def test_progress_counts_only_same_category_currency_and_inclusive_period() -> None:
    service, store = make_service()
    weekly = create_budget(service)
    monthly = create_budget(
        service,
        category_id=11,
        currency="USD",
        period=BudgetPeriod.MONTHLY,
        amount="40.00",
    )
    store.spending.extend(
        [
            DailySpendingRecord(10, COP.id, date(2026, 9, 21), Decimal("10.00")),
            DailySpendingRecord(10, COP.id, date(2026, 9, 27), Decimal("15.00")),
            DailySpendingRecord(10, USD.id, date(2026, 9, 25), Decimal("99.00")),
            DailySpendingRecord(11, COP.id, date(2026, 9, 25), Decimal("77.00")),
            DailySpendingRecord(11, USD.id, date(2026, 9, 1), Decimal("50.00")),
        ]
    )

    result = service.list(owner_id=7, preferred_currency_code="COP")
    by_id = {item.id: item for item in result.budgets}

    assert by_id[weekly.id].spent_amount == Decimal("25.00")
    assert by_id[weekly.id].progress_percent == Decimal("25.00")
    assert by_id[monthly.id].spent_amount == Decimal("50.00")
    assert by_id[monthly.id].remaining_amount == Decimal("-10.00")
    assert len(result.budgets) == 2
    assert [item.id for item in result.categories] == [10, 11]


def test_update_delete_and_foreign_access_preserve_ownership() -> None:
    service, store = make_service()
    budget = create_budget(service)

    updated = service.update(
        owner_id=7,
        budget_id=budget.id,
        command=BudgetUpdateCommand(
            category_id=11,
            amount=Decimal("250.00"),
            currency_code="USD",
            period=BudgetPeriod.MONTHLY,
        ),
    )

    assert updated.category is not None and updated.category.id == 11
    assert updated.amount == Decimal("250.00")
    assert updated.currency.code == "USD"
    assert updated.starts_on == date(2026, 9, 1)
    with pytest.raises(BudgetNotFoundError):
        service.get(owner_id=8, budget_id=budget.id)
    with pytest.raises(BudgetNotFoundError):
        service.delete(owner_id=8, budget_id=budget.id)
    service.delete(owner_id=7, budget_id=budget.id)
    assert budget.id not in store.budgets


def test_foreign_category_and_unsupported_currency_are_rejected() -> None:
    service, _ = make_service()

    with pytest.raises(BudgetCategoryUnavailableError):
        create_budget(service, category_id=20)
    with pytest.raises(BudgetCurrencyInvalidError):
        create_budget(service, currency="EUR")


def test_advanced_schedule_supports_custom_name_all_expenses_and_date_bounds() -> None:
    service, store = make_service()
    created = service.create(
        owner_id=7,
        command=BudgetCreateCommand(
            category_id=None,
            name="  Family   fortnight  ",
            amount=Decimal("300.00"),
            currency_code="COP",
            schedule=BudgetScheduleCommand(
                schedule_type=ScheduleType.RECURRING,
                frequency_unit=FrequencyUnit.WEEK,
                frequency_interval=2,
                starts_on=date(2026, 9, 14),
                ends_on=date(2026, 10, 31),
            ),
        ),
    )
    store.spending.extend(
        [
            DailySpendingRecord(10, COP.id, date(2026, 9, 21), Decimal("25.00")),
            DailySpendingRecord(11, COP.id, date(2026, 9, 27), Decimal("35.00")),
        ]
    )

    result = service.get(owner_id=7, budget_id=created.id)

    assert result.name == "Family fortnight"
    assert result.category is None
    assert result.frequency_interval == 2
    assert result.current_period_start == date(2026, 9, 14)
    assert result.current_period_end == date(2026, 9, 27)
    assert result.spent_amount == Decimal("60.00")


def test_update_can_change_to_one_time_all_expenses_range() -> None:
    service, _ = make_service()
    budget = create_budget(service)

    updated = service.update(
        owner_id=7,
        budget_id=budget.id,
        command=BudgetUpdateCommand(
            category_changed=True,
            category_id=None,
            name_changed=True,
            name="Trip",
            schedule=BudgetScheduleCommand(
                schedule_type=ScheduleType.ONE_TIME,
                frequency_unit=None,
                frequency_interval=None,
                starts_on=date(2026, 12, 1),
                ends_on=date(2026, 12, 20),
            ),
        ),
    )

    assert updated.name == "Trip"
    assert updated.category is None
    assert updated.schedule_type is ScheduleType.ONE_TIME
    assert updated.frequency_unit is None
    assert updated.starts_on == date(2026, 12, 1)
    assert updated.ends_on == date(2026, 12, 20)

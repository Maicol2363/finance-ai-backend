"""EML-03 rules: which budgets deserve an email and for which threshold. Pure functions, no
database: persistence and concurrency are proved in the PostgreSQL integration tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finance_ai.accounts.schemas import CurrencySummary
from finance_ai.budgets.periods import FrequencyUnit, PeriodRange, ScheduleType
from finance_ai.budgets.repositories import (
    BudgetCategoryRecord,
    BudgetRecord,
    DailySpendingRecord,
)
from finance_ai.notifications.budget_alerts import current_periods, dedupe_key, due_alerts

COP = CurrencySummary(1, "COP", "Peso colombiano", 2)
USD = CurrencySummary(2, "USD", "US dollar", 2)
FOOD = BudgetCategoryRecord(id=7, user_id=1, name="Comida", color="orange", icon="food")
TODAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 1, tzinfo=UTC)


def budget(
    *,
    amount: str = "600000.00",
    category: BudgetCategoryRecord | None = None,
    currency: CurrencySummary = COP,
    schedule_type: ScheduleType = ScheduleType.RECURRING,
    unit: FrequencyUnit | None = FrequencyUnit.MONTH,
    starts_on: date = date(2026, 11, 1),
    ends_on: date | None = None,
    is_active: bool = True,
    budget_id: int = 5,
) -> BudgetRecord:
    return BudgetRecord(
        id=budget_id,
        user_id=1,
        currency=currency,
        category=category,
        name="Mercado",
        amount=Decimal(amount),
        schedule_type=schedule_type,
        frequency_unit=unit if schedule_type is ScheduleType.RECURRING else None,
        frequency_interval=1 if schedule_type is ScheduleType.RECURRING else None,
        starts_on=starts_on,
        ends_on=ends_on,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def spend(
    amount: str, *, on: date = date(2026, 11, 10), category: int | None = 7, currency: int = 1
) -> DailySpendingRecord:
    return DailySpendingRecord(
        category_id=category, currency_id=currency, spent_on=on, amount=Decimal(amount)
    )


def due(record: BudgetRecord, *spending: DailySpendingRecord, today: date = TODAY):  # type: ignore[no-untyped-def]
    return due_alerts(current_periods([record], today), list(spending))


# --- thresholds ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spent", "threshold", "superseded"),
    [
        ("0", None, ()),
        ("479999.99", None, ()),
        ("480000.00", 80, ()),  # exactly 80 % counts
        ("599999.99", 80, ()),
        ("600000.00", 100, (80,)),  # exactly the limit
        ("750000.00", 100, (80,)),  # over the limit
    ],
)
def test_the_highest_threshold_reached_is_the_only_one_worth_an_email(
    spent: str, threshold: int | None, superseded: tuple[int, ...]
) -> None:
    result = due(budget(), spend(spent))

    if threshold is None:
        assert result == []
    else:
        [alert] = result
        assert (alert.threshold, alert.superseded) == (threshold, superseded)
        assert alert.spent == Decimal(spent)


def test_one_big_expense_that_jumps_both_thresholds_supersedes_the_lower_one() -> None:
    [alert] = due(budget(), spend("900000.00"))

    assert alert.threshold == 100 and alert.superseded == (80,)


# --- what counts as spending ---------------------------------------------------------------


def test_an_all_expenses_budget_counts_every_category() -> None:
    [alert] = due(budget(), spend("300000", category=7), spend("200000", category=None))

    assert alert.threshold == 80 and alert.spent == Decimal("500000")


def test_a_category_budget_counts_only_its_category() -> None:
    record = budget(category=FOOD)

    assert due(record, spend("500000", category=8)) == []
    [alert] = due(record, spend("500000", category=7))
    assert alert.threshold == 80


def test_other_currencies_do_not_count() -> None:
    assert due(budget(), spend("900000", currency=2)) == []
    [alert] = due(budget(currency=USD, amount="100"), spend("90", currency=2))
    assert alert.threshold == 80


def test_spending_outside_the_current_period_does_not_count() -> None:
    last_month = spend("900000", on=date(2026, 10, 20))

    assert due(budget(), last_month) == []  # a new month starts from zero


# --- which budgets are looked at -----------------------------------------------------------


def test_an_inactive_budget_is_never_warned_about() -> None:
    assert due(budget(is_active=False), spend("900000")) == []


def test_a_budget_that_has_not_started_yet_is_not_warned_about() -> None:
    record = budget(starts_on=date(2026, 12, 1))

    assert current_periods([record], TODAY) == []


def test_a_budget_that_already_ended_is_not_warned_about() -> None:
    record = budget(starts_on=date(2026, 9, 1), ends_on=date(2026, 10, 31))

    assert current_periods([record], TODAY) == []


def test_a_one_time_budget_is_warned_about_only_inside_its_dates() -> None:
    record = budget(
        schedule_type=ScheduleType.ONE_TIME,
        unit=None,
        starts_on=date(2026, 11, 10),
        ends_on=date(2026, 11, 20),
    )

    [(found, period)] = current_periods([record], TODAY)
    assert found is record and period == PeriodRange(date(2026, 11, 10), date(2026, 11, 20))
    assert current_periods([record], date(2026, 11, 21)) == []
    assert current_periods([record], date(2026, 11, 9)) == []


def test_each_budget_is_judged_on_its_own_period() -> None:
    weekly = budget(
        budget_id=6, unit=FrequencyUnit.WEEK, starts_on=date(2026, 11, 9), amount="100000.00"
    )
    monthly = budget(budget_id=5)
    # 16 Nov is a Monday: it opens a new week, so the 12th would belong to the previous one.
    spending = [spend("90000", on=date(2026, 11, 16)), spend("10000", on=date(2026, 11, 2))]

    alerts = due_alerts(current_periods([monthly, weekly], TODAY), spending)

    assert [(a.record.id, a.threshold) for a in alerts] == [(6, 80)]  # 90 000 of 100 000 this week


# --- the key that makes a decision happen once ---------------------------------------------


def test_the_key_is_stable_and_changes_with_period_threshold_or_limit() -> None:
    record = budget()
    base = dedupe_key(record, date(2026, 11, 1), 80)

    assert base == "budget:5:2026-11-01:80:600000.00"
    assert base == dedupe_key(budget(), date(2026, 11, 1), 80)
    assert base != dedupe_key(record, date(2026, 12, 1), 80)  # a new period re-arms
    assert base != dedupe_key(record, date(2026, 11, 1), 100)
    assert base != dedupe_key(budget(amount="700000.00"), date(2026, 11, 1), 80)  # new limit
    assert base != dedupe_key(budget(budget_id=6), date(2026, 11, 1), 80)

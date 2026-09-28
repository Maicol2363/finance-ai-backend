from datetime import date

from finance_ai.budgets.periods import (
    BudgetPeriod,
    FrequencyUnit,
    ScheduleType,
    default_start,
    resolve_period,
)


def test_default_week_and_month_are_calendar_aligned() -> None:
    today = date(2026, 9, 27)

    assert default_start(BudgetPeriod.WEEKLY, today) == date(2026, 9, 21)
    assert default_start(BudgetPeriod.MONTHLY, today) == date(2026, 9, 1)


def test_weekly_and_multi_month_occurrences_are_inclusive() -> None:
    weekly = resolve_period(
        schedule_type=ScheduleType.RECURRING,
        frequency_unit=FrequencyUnit.WEEK,
        frequency_interval=1,
        starts_on=date(2026, 9, 7),
        ends_on=None,
        as_of=date(2026, 9, 27),
    )
    quarterly = resolve_period(
        schedule_type=ScheduleType.RECURRING,
        frequency_unit=FrequencyUnit.MONTH,
        frequency_interval=3,
        starts_on=date(2026, 1, 31),
        ends_on=None,
        as_of=date(2026, 5, 15),
    )

    assert (weekly.starts_on, weekly.ends_on) == (
        date(2026, 9, 21),
        date(2026, 9, 27),
    )
    assert (quarterly.starts_on, quarterly.ends_on) == (
        date(2026, 4, 30),
        date(2026, 7, 30),
    )


def test_daily_yearly_one_time_and_global_end_are_supported() -> None:
    daily = resolve_period(
        schedule_type=ScheduleType.RECURRING,
        frequency_unit=FrequencyUnit.DAY,
        frequency_interval=2,
        starts_on=date(2026, 9, 1),
        ends_on=date(2026, 9, 8),
        as_of=date(2026, 9, 20),
    )
    yearly = resolve_period(
        schedule_type=ScheduleType.RECURRING,
        frequency_unit=FrequencyUnit.YEAR,
        frequency_interval=1,
        starts_on=date(2024, 2, 29),
        ends_on=None,
        as_of=date(2026, 3, 1),
    )
    one_time = resolve_period(
        schedule_type=ScheduleType.ONE_TIME,
        frequency_unit=None,
        frequency_interval=None,
        starts_on=date(2026, 10, 2),
        ends_on=date(2026, 10, 12),
        as_of=date(2026, 10, 7),
    )

    assert (daily.starts_on, daily.ends_on) == (date(2026, 9, 7), date(2026, 9, 8))
    assert (yearly.starts_on, yearly.ends_on) == (
        date(2026, 2, 28),
        date(2027, 2, 27),
    )
    assert (one_time.starts_on, one_time.ends_on) == (
        date(2026, 10, 2),
        date(2026, 10, 12),
    )

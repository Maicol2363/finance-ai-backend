from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class ScheduleType(StrEnum):
    RECURRING = "recurring"
    ONE_TIME = "one_time"


class FrequencyUnit(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class BudgetPeriod(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class PeriodRange:
    starts_on: date
    ends_on: date


def default_start(period: BudgetPeriod, today: date) -> date:
    if period is BudgetPeriod.WEEKLY:
        return today - timedelta(days=today.weekday())
    return today.replace(day=1)


def period_frequency(period: BudgetPeriod) -> FrequencyUnit:
    return FrequencyUnit.WEEK if period is BudgetPeriod.WEEKLY else FrequencyUnit.MONTH


def resolve_period(
    *,
    schedule_type: ScheduleType,
    frequency_unit: FrequencyUnit | None,
    frequency_interval: int | None,
    starts_on: date,
    ends_on: date | None,
    as_of: date,
) -> PeriodRange:
    """Resolve the occurrence shown by the dashboard without storing progress.

    The resolver supports every schedule shape represented by the database even
    though HG-016's default form currently creates weekly or monthly budgets.
    """
    if schedule_type is ScheduleType.ONE_TIME:
        return PeriodRange(starts_on, ends_on or starts_on)
    if frequency_unit is None or frequency_interval is None or frequency_interval < 1:
        raise ValueError("A recurring budget requires a positive frequency")

    reference = as_of
    if reference < starts_on:
        reference = starts_on
    if ends_on is not None and reference > ends_on:
        reference = ends_on

    occurrence = _occurrence_start(
        anchor=starts_on,
        reference=reference,
        unit=frequency_unit,
        interval=frequency_interval,
    )
    next_occurrence = _add_units(
        anchor=starts_on,
        count=_occurrence_index(
            anchor=starts_on,
            reference=occurrence,
            unit=frequency_unit,
            interval=frequency_interval,
        )
        + 1,
        unit=frequency_unit,
        interval=frequency_interval,
    )
    occurrence_end = next_occurrence - timedelta(days=1)
    if ends_on is not None:
        occurrence_end = min(occurrence_end, ends_on)
    return PeriodRange(occurrence, occurrence_end)


def _occurrence_start(*, anchor: date, reference: date, unit: FrequencyUnit, interval: int) -> date:
    index = _occurrence_index(
        anchor=anchor,
        reference=reference,
        unit=unit,
        interval=interval,
    )
    candidate = _add_units(anchor=anchor, count=index, unit=unit, interval=interval)
    if candidate > reference and index > 0:
        return _add_units(anchor=anchor, count=index - 1, unit=unit, interval=interval)
    return candidate


def _occurrence_index(*, anchor: date, reference: date, unit: FrequencyUnit, interval: int) -> int:
    if reference <= anchor:
        return 0
    if unit is FrequencyUnit.DAY:
        return (reference - anchor).days // interval
    if unit is FrequencyUnit.WEEK:
        return (reference - anchor).days // (7 * interval)
    month_delta = (reference.year - anchor.year) * 12 + reference.month - anchor.month
    months_per_occurrence = interval * (12 if unit is FrequencyUnit.YEAR else 1)
    return max(month_delta // months_per_occurrence, 0)


def _add_units(*, anchor: date, count: int, unit: FrequencyUnit, interval: int) -> date:
    steps = count * interval
    if unit is FrequencyUnit.DAY:
        return anchor + timedelta(days=steps)
    if unit is FrequencyUnit.WEEK:
        return anchor + timedelta(weeks=steps)
    months = steps * (12 if unit is FrequencyUnit.YEAR else 1)
    total_month = anchor.year * 12 + (anchor.month - 1) + months
    year, month_index = divmod(total_month, 12)
    month = month_index + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

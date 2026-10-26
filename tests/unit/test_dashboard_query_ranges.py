from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from finance_ai.dashboard.repository import DashboardRepository


class _Result:
    def one(self) -> tuple[Decimal, Decimal]:
        return Decimal("0"), Decimal("0")


class _Session:
    def __init__(self) -> None:
        self.statement = ""
        self.params: dict[str, Any] = {}

    def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.statement = str(statement)
        self.params = params
        return _Result()


def test_projection_query_bounds_expense_scan_to_its_90_day_window() -> None:
    session = _Session()
    repository = DashboardRepository(session)  # type: ignore[arg-type]

    repository.get_projection_data(
        user_id=7,
        currency="COP",
        month_start=date(2026, 10, 1),
        today=date(2026, 10, 19),
        tz="America/Bogota",
    )

    assert "AT TIME ZONE 'UTC') - interval '36 hours'" in session.statement
    assert "AT TIME ZONE 'UTC') + interval '36 hours'" in session.statement
    assert "day BETWEEN :month_start AND :today" in session.statement
    assert "day BETWEEN :month_start - 90 AND :month_start - 1" in session.statement


def test_runway_and_weekday_queries_bound_expense_scan_to_consumed_periods() -> None:
    session = _Session()
    repository = DashboardRepository(session)  # type: ignore[arg-type]

    repository.get_balance_and_runway(
        user_id=7, currency="COP", today=date(2026, 10, 19), tz="America/Bogota"
    )
    assert "AT TIME ZONE :tz" in session.statement
    assert "interval '3 months'" in session.statement
    assert "ft.occurred_at >= (" in session.statement
    assert "ft.occurred_at < (" in session.statement

    repository.get_weekday_pattern(
        user_id=7, currency="COP", today=date(2026, 10, 19), tz="America/Bogota"
    )
    assert "CAST(:today - 89 AS timestamp) AT TIME ZONE 'UTC'" in session.statement
    assert "CAST(:today + 1 AS timestamp) AT TIME ZONE 'UTC'" in session.statement
    assert "LEFT JOIN spend s ON s.day = days.day" in session.statement

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from finance_ai.dashboard.errors import (
    DashboardCurrencyInvalidError,
    DashboardDateRangeInvalidError,
)
from finance_ai.dashboard.repository import DashboardRepository
from finance_ai.dashboard.service import DashboardService


class StubDashboardRepository(DashboardRepository):
    def __init__(self) -> None:
        self.valid_currencies = {"COP", "USD"}
        self.flow_incomes = Decimal("5000000.00")
        self.flow_expenses = Decimal("2000000.00")
        self.period_expenses_map: dict[tuple[date, date], Decimal] = {}
        self.weekly_spend: list[dict[str, Any]] = [
            {"start": date(2026, 9, 1), "end": date(2026, 9, 7), "expenses": Decimal("500000.00")},
            {"start": date(2026, 9, 8), "end": date(2026, 9, 14), "expenses": Decimal("400000.00")},
        ]
        self.top_categories: list[dict[str, Any]] = [
            {"category_id": 1, "name": "Vivienda", "total": Decimal("1200000.00")},
            {"category_id": 2, "name": "Alimentación", "total": Decimal("600000.00")},
        ]
        self.spent_mtd = Decimal("1500000.00")
        self.hist_daily = Decimal("60000.00")
        self.balance = Decimal("8500000.00")
        self.avg_monthly_spend = Decimal("2100000.00")
        self.runway_months: Decimal | None = Decimal("4.0")
        self.avg_3m = Decimal("4800000.00")
        self.avg_6m = Decimal("4500000.00")
        self.variability: Decimal | None = Decimal("0.18")
        self.weekend_avg = Decimal("120000.00")
        self.weekday_avg = Decimal("45000.00")
        self.recurring_candidates: list[dict[str, Any]] = [
            {
                "merchant": "netflix",
                "typical_amount": Decimal("49900.00"),
                "next_expected": date(2026, 10, 15),
            }
        ]
        self.recent_movements: list[dict[str, Any]] = [
            {
                "id": 101,
                "type": "expense",
                "occurred_at": datetime(2026, 10, 3, 14, 30),
                "title": "Supermercado Exito",
                "amount": Decimal("-150000.00"),
            }
        ]

    def validate_currency(self, currency_code: str) -> bool:
        return currency_code in self.valid_currencies

    def get_flow_totals(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
    ) -> tuple[Decimal, Decimal]:
        return self.flow_incomes, self.flow_expenses

    def get_period_expenses(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
    ) -> Decimal:
        return self.period_expenses_map.get((start_date, end_date), Decimal("800000.00"))

    def get_weekly_spend(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
    ) -> list[dict[str, Any]]:
        return self.weekly_spend

    def get_top_categories(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return self.top_categories

    def get_projection_data(
        self,
        *,
        user_id: int,
        currency: str,
        month_start: date,
        today: date,
        tz: str,
    ) -> tuple[Decimal, Decimal]:
        return self.spent_mtd, self.hist_daily

    def get_balance_and_runway(
        self,
        *,
        user_id: int,
        currency: str,
        today: date,
        tz: str,
    ) -> tuple[Decimal, Decimal, Decimal | None]:
        return self.balance, self.avg_monthly_spend, self.runway_months

    def get_income_average(
        self,
        *,
        user_id: int,
        currency: str,
        today: date,
    ) -> tuple[Decimal, Decimal, Decimal | None]:
        return self.avg_3m, self.avg_6m, self.variability

    def get_weekday_pattern(
        self,
        *,
        user_id: int,
        currency: str,
        today: date,
        tz: str,
    ) -> tuple[Decimal, Decimal]:
        return self.weekend_avg, self.weekday_avg

    def get_recurring_candidates(
        self,
        *,
        user_id: int,
        today: date,
        tz: str,
    ) -> list[dict[str, Any]]:
        return self.recurring_candidates

    def get_recent_movements(
        self,
        *,
        user_id: int,
        currency: str,
        start_date: date,
        end_date: date,
        tz: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return self.recent_movements


def test_dashboard_service_success() -> None:
    repo = StubDashboardRepository()
    service = DashboardService(repo)

    summary = service.get_summary(
        user_id=1,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        currency="COP",
    )

    assert summary.period.start == date(2026, 10, 1)
    assert summary.period.end == date(2026, 10, 31)
    assert summary.flow.incomes == "5000000.00"
    assert summary.flow.expenses == "2000000.00"
    assert summary.flow.net == "3000000.00"
    # 2 top + "Otras" (1200000 + 600000 = 1800000, 200000 other)
    assert len(summary.top_categories) == 3
    assert summary.top_categories[-1].name == "Otras"
    assert summary.top_categories[-1].total == "200000.00"
    assert summary.balance.total == "8500000.00"
    assert summary.balance.runway_months == "4.0"
    assert summary.income_average.avg_3m == "4800000.00"
    assert summary.weekday_pattern.weekend_avg == "120000.00"
    assert len(summary.recurring_candidates) == 1
    assert summary.recurring_candidates[0].merchant == "netflix"
    assert len(summary.recent) == 1
    assert summary.recent[0].title == "Supermercado Exito"


def test_dashboard_service_invalid_currency() -> None:
    repo = StubDashboardRepository()
    service = DashboardService(repo)

    with pytest.raises(DashboardCurrencyInvalidError):
        service.get_summary(
            user_id=1,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
            currency="EUR",
        )


def test_dashboard_service_invalid_date_range() -> None:
    repo = StubDashboardRepository()
    service = DashboardService(repo)

    with pytest.raises(DashboardDateRangeInvalidError):
        service.get_summary(
            user_id=1,
            start_date=date(2026, 10, 31),
            end_date=date(2026, 10, 1),
            currency="COP",
        )

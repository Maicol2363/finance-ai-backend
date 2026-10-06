from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from finance_ai.accounts.schemas import format_money
from finance_ai.dashboard.errors import (
    DashboardCurrencyInvalidError,
    DashboardDateRangeInvalidError,
)
from finance_ai.dashboard.repository import DashboardRepository
from finance_ai.dashboard.schemas import (
    BalanceSummary,
    ComparisonSummary,
    DashboardSummaryResponse,
    FlowSummary,
    IncomeAverageSummary,
    PeriodSummary,
    ProjectionSummary,
    RecentMovementItem,
    RecurringCandidate,
    TopCategoryItem,
    WeekdayPattern,
    WeeklySpendItem,
)


class DashboardService:
    def __init__(self, repository: DashboardRepository) -> None:
        self.repository = repository

    def get_summary(
        self,
        *,
        user_id: int,
        start_date: date,
        end_date: date,
        currency: str = "COP",
        tz: str = "America/Bogota",
    ) -> DashboardSummaryResponse:
        if start_date > end_date:
            raise DashboardDateRangeInvalidError("start_date must be on or before end_date.")

        if not self.repository.validate_currency(currency):
            raise DashboardCurrencyInvalidError(currency)

        try:
            zone = ZoneInfo(tz)
        except Exception:
            zone = ZoneInfo("America/Bogota")
            tz = "America/Bogota"

        today = datetime.now(zone).date()

        # 1. Period
        days_total = (end_date - start_date).days + 1
        is_current = start_date <= today <= end_date

        if is_current:
            days_elapsed = (today - start_date).days + 1
        elif today > end_date:
            days_elapsed = days_total
        else:
            days_elapsed = 0

        period = PeriodSummary(
            start=start_date,
            end=end_date,
            today=today,
            is_current=is_current,
            days_elapsed=days_elapsed,
            days_total=days_total,
        )

        # 2. Flow
        incomes_dec, expenses_dec = self.repository.get_flow_totals(
            user_id=user_id,
            currency=currency,
            start_date=start_date,
            end_date=end_date,
            tz=tz,
        )
        net_dec = incomes_dec - expenses_dec
        flow = FlowSummary(
            incomes=format_money(incomes_dec),
            expenses=format_money(expenses_dec),
            net=format_money(net_dec),
        )

        # 3. Comparison window
        comparison_days = days_elapsed if (is_current and days_elapsed > 0) else days_total

        _, cur_month_days = calendar.monthrange(start_date.year, start_date.month)
        is_full_month_window = start_date.day == 1 and end_date == date(
            start_date.year, start_date.month, cur_month_days
        )

        if is_full_month_window:
            if start_date.month == 1:
                prev_month_start = date(start_date.year - 1, 12, 1)
            else:
                prev_month_start = date(start_date.year, start_date.month - 1, 1)

            _, prev_month_total_days = calendar.monthrange(
                prev_month_start.year, prev_month_start.month
            )
            previous_start = prev_month_start
            if is_current:
                previous_end = prev_month_start + timedelta(
                    days=min(days_elapsed, prev_month_total_days) - 1
                )
            else:
                previous_end = date(
                    prev_month_start.year, prev_month_start.month, prev_month_total_days
                )
        else:
            previous_end = start_date - timedelta(days=1)
            previous_start = previous_end - timedelta(days=comparison_days - 1)

        if is_current:
            current_spend_dec = self.repository.get_period_expenses(
                user_id=user_id,
                currency=currency,
                start_date=start_date,
                end_date=today,
                tz=tz,
            )
        else:
            current_spend_dec = expenses_dec

        prev_spend_dec = self.repository.get_period_expenses(
            user_id=user_id,
            currency=currency,
            start_date=previous_start,
            end_date=previous_end,
            tz=tz,
        )

        if prev_spend_dec > Decimal("0"):
            diff = current_spend_dec - prev_spend_dec
            pct_val = (diff / prev_spend_dec) * Decimal("100")
            pct_str = f"{abs(pct_val):.1f}"
            if diff > Decimal("0"):
                direction = "up"
            elif diff < Decimal("0"):
                direction = "down"
            else:
                direction = "flat"
        else:
            pct_str = None
            direction = "up" if current_spend_dec > Decimal("0") else "none"

        comparison = ComparisonSummary(
            current=format_money(current_spend_dec),
            previous=format_money(prev_spend_dec),
            previous_start=previous_start,
            previous_end=previous_end,
            pct=pct_str,
            direction=direction,
        )

        # 4. Weekly spend
        weekly_rows = self.repository.get_weekly_spend(
            user_id=user_id,
            currency=currency,
            start_date=start_date,
            end_date=end_date,
            tz=tz,
        )
        weekly = [
            WeeklySpendItem(
                start=w["start"],
                end=w["end"],
                expenses=format_money(w["expenses"]),
                is_future=w["start"] > today,
            )
            for w in weekly_rows
        ]

        # 5. Top categories
        cat_rows = self.repository.get_top_categories(
            user_id=user_id,
            currency=currency,
            start_date=start_date,
            end_date=end_date,
            tz=tz,
            limit=5,
        )
        top_categories: list[TopCategoryItem] = []
        top_sum = Decimal("0")
        for cat in cat_rows:
            top_sum += cat["total"]
            share = (
                (cat["total"] / expenses_dec * Decimal("100"))
                if expenses_dec > Decimal("0")
                else Decimal("0")
            )
            top_categories.append(
                TopCategoryItem(
                    category_id=cat["category_id"],
                    name=cat["name"],
                    total=format_money(cat["total"]),
                    share_pct=f"{share:.1f}",
                )
            )

        remaining_other = expenses_dec - top_sum
        if remaining_other > Decimal("0.009") and expenses_dec > Decimal("0"):
            other_share = remaining_other / expenses_dec * Decimal("100")
            top_categories.append(
                TopCategoryItem(
                    category_id=None,
                    name="Otras",
                    total=format_money(remaining_other),
                    share_pct=f"{other_share:.1f}",
                )
            )

        # 6. Projection
        month_start = today.replace(day=1)
        _, days_in_cur_month = calendar.monthrange(today.year, today.month)
        spent_mtd, hist_daily = self.repository.get_projection_data(
            user_id=user_id,
            currency=currency,
            month_start=month_start,
            today=today,
            tz=tz,
        )

        elapsed_m_days = today.day
        remaining_m_days = days_in_cur_month - elapsed_m_days

        w = Decimal(elapsed_m_days) / Decimal(days_in_cur_month)
        mtd_daily = (spent_mtd / Decimal(elapsed_m_days)) if elapsed_m_days > 0 else Decimal("0")
        projected_remaining = (w * mtd_daily + (Decimal("1") - w) * hist_daily) * Decimal(
            remaining_m_days
        )
        projected_total = spent_mtd + projected_remaining

        projection = ProjectionSummary(
            spent_mtd=format_money(spent_mtd),
            projected=format_money(projected_total),
        )

        # 7. Balance and runway
        bal_total, avg_monthly_spend, runway = self.repository.get_balance_and_runway(
            user_id=user_id,
            currency=currency,
            today=today,
            tz=tz,
        )
        balance = BalanceSummary(
            total=format_money(bal_total),
            avg_monthly_spend=format_money(avg_monthly_spend),
            runway_months=f"{runway:.1f}" if runway is not None else None,
        )

        # 8. Income average
        avg_3m, avg_6m, variability = self.repository.get_income_average(
            user_id=user_id,
            currency=currency,
            today=today,
        )
        income_average = IncomeAverageSummary(
            avg_3m=format_money(avg_3m),
            avg_6m=format_money(avg_6m),
            variability=f"{variability:.2f}" if variability is not None else None,
        )

        # 9. Weekday pattern
        weekend_avg, weekday_avg = self.repository.get_weekday_pattern(
            user_id=user_id,
            currency=currency,
            today=today,
            tz=tz,
        )
        weekday_pattern = WeekdayPattern(
            weekend_avg=format_money(weekend_avg),
            weekday_avg=format_money(weekday_avg),
        )

        # 10. Recurring candidates
        rec_rows = self.repository.get_recurring_candidates(
            user_id=user_id,
            today=today,
            tz=tz,
        )
        recurring_candidates = [
            RecurringCandidate(
                merchant=r["merchant"],
                typical_amount=format_money(r["typical_amount"]),
                next_expected=r["next_expected"],
            )
            for r in rec_rows
        ]

        # 11. Recent movements
        rec_moves = self.repository.get_recent_movements(
            user_id=user_id,
            currency=currency,
            start_date=start_date,
            end_date=end_date,
            tz=tz,
            limit=5,
        )
        recent = [
            RecentMovementItem(
                id=m["id"],
                type=m["type"],
                occurred_at=m["occurred_at"],
                title=m["title"],
                amount=format_money(m["amount"]),
            )
            for m in rec_moves
        ]

        return DashboardSummaryResponse(
            period=period,
            flow=flow,
            comparison=comparison,
            weekly=weekly,
            top_categories=top_categories,
            projection=projection,
            balance=balance,
            income_average=income_average,
            weekday_pattern=weekday_pattern,
            recurring_candidates=recurring_candidates,
            recent=recent,
        )

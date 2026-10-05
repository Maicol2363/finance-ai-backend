from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class PeriodSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    today: date
    is_current: bool
    days_elapsed: int
    days_total: int


class FlowSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incomes: str
    expenses: str
    net: str


class ComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current: str
    previous: str
    previous_start: date
    previous_end: date
    pct: str | None = None
    direction: str  # "up", "down", "flat", "none"


class WeeklySpendItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    expenses: str
    is_future: bool


class TopCategoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: int | None = None
    name: str
    total: str
    share_pct: str


class ProjectionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spent_mtd: str
    projected: str


class BalanceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: str
    avg_monthly_spend: str
    runway_months: str | None = None


class IncomeAverageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    avg_3m: str
    avg_6m: str
    variability: str | None = None


class WeekdayPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekend_avg: str
    weekday_avg: str


class RecurringCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant: str
    typical_amount: str
    next_expected: date


class RecentMovementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    type: str
    occurred_at: datetime
    title: str
    amount: str


class DashboardSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: PeriodSummary
    flow: FlowSummary
    comparison: ComparisonSummary
    weekly: list[WeeklySpendItem]
    top_categories: list[TopCategoryItem]
    projection: ProjectionSummary
    balance: BalanceSummary
    income_average: IncomeAverageSummary
    weekday_pattern: WeekdayPattern
    recurring_candidates: list[RecurringCandidate]
    recent: list[RecentMovementItem]

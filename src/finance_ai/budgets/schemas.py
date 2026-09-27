from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from finance_ai.accounts.schemas import CurrencyResponse, CurrencySummary, MoneyInput, format_money
from finance_ai.budgets.periods import BudgetPeriod, FrequencyUnit, ScheduleType


@dataclass(frozen=True)
class BudgetCategoryView:
    id: int
    name: str
    color: str
    icon: str


@dataclass(frozen=True)
class BudgetView:
    id: int
    category: BudgetCategoryView | None
    currency: CurrencySummary
    amount: Decimal
    schedule_type: ScheduleType
    frequency_unit: FrequencyUnit | None
    frequency_interval: int | None
    starts_on: date
    ends_on: date | None
    current_period_start: date
    current_period_end: date
    spent_amount: Decimal
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @property
    def remaining_amount(self) -> Decimal:
        return self.amount - self.spent_amount

    @property
    def progress_percent(self) -> Decimal:
        return (self.spent_amount / self.amount * Decimal("100")).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class BudgetListResult:
    budgets: tuple[BudgetView, ...]
    categories: tuple[BudgetCategoryView, ...]
    supported_currencies: tuple[CurrencySummary, ...]
    suggested_currency_code: str


@dataclass(frozen=True)
class BudgetCreateCommand:
    category_id: int
    amount: Decimal
    currency_code: str
    period: BudgetPeriod


@dataclass(frozen=True)
class BudgetUpdateCommand:
    category_id: int | None = None
    amount: Decimal | None = None
    currency_code: str | None = None
    period: BudgetPeriod | None = None


class BudgetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: int = Field(gt=0)
    amount: MoneyInput = Field(gt=0)
    currency_code: str = Field(min_length=3, max_length=10)
    period: BudgetPeriod

    def to_command(self) -> BudgetCreateCommand:
        return BudgetCreateCommand(
            category_id=self.category_id,
            amount=self.amount,
            currency_code=self.currency_code,
            period=self.period,
        )


class BudgetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: Annotated[int, Field(gt=0)] | SkipJsonSchema[None] = None
    amount: MoneyInput | SkipJsonSchema[None] = None
    currency_code: Annotated[str, Field(min_length=3, max_length=10)] | SkipJsonSchema[None] = None
    period: BudgetPeriod | SkipJsonSchema[None] = None

    @field_validator("category_id", "amount", "currency_code", "period", mode="after")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field cannot be null")
        return value

    @model_validator(mode="after")
    def require_field(self) -> BudgetUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        if self.amount is not None and self.amount <= 0:
            raise ValueError("Amount must be positive")
        return self

    def to_command(self) -> BudgetUpdateCommand:
        fields = self.model_fields_set
        return BudgetUpdateCommand(
            category_id=self.category_id if "category_id" in fields else None,
            amount=self.amount if "amount" in fields else None,
            currency_code=self.currency_code if "currency_code" in fields else None,
            period=self.period if "period" in fields else None,
        )


class BudgetCategoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    color: str
    icon: str

    @classmethod
    def from_result(cls, value: BudgetCategoryView) -> BudgetCategoryResponse:
        return cls(id=value.id, name=value.name, color=value.color, icon=value.icon)


class BudgetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    category: BudgetCategoryResponse | None
    currency: CurrencyResponse
    amount: str
    schedule_type: ScheduleType
    frequency_unit: FrequencyUnit | None
    frequency_interval: int | None
    starts_on: date
    ends_on: date | None
    current_period_start: date
    current_period_end: date
    spent_amount: str
    remaining_amount: str
    progress_percent: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_result(cls, value: BudgetView) -> BudgetResponse:
        return cls(
            id=value.id,
            category=(
                None
                if value.category is None
                else BudgetCategoryResponse.from_result(value.category)
            ),
            currency=CurrencyResponse.from_result(value.currency),
            amount=format_money(value.amount),
            schedule_type=value.schedule_type,
            frequency_unit=value.frequency_unit,
            frequency_interval=value.frequency_interval,
            starts_on=value.starts_on,
            ends_on=value.ends_on,
            current_period_start=value.current_period_start,
            current_period_end=value.current_period_end,
            spent_amount=format_money(value.spent_amount),
            remaining_amount=format_money(value.remaining_amount),
            progress_percent=format(value.progress_percent, ".2f"),
            is_active=value.is_active,
            created_at=value.created_at,
            updated_at=value.updated_at,
        )


class BudgetListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budgets: list[BudgetResponse]
    categories: list[BudgetCategoryResponse]
    supported_currencies: list[CurrencyResponse]
    suggested_currency_code: str

    @classmethod
    def from_result(cls, value: BudgetListResult) -> BudgetListResponse:
        return cls(
            budgets=[BudgetResponse.from_result(item) for item in value.budgets],
            categories=[BudgetCategoryResponse.from_result(item) for item in value.categories],
            supported_currencies=[
                CurrencyResponse.from_result(item) for item in value.supported_currencies
            ],
            suggested_currency_code=value.suggested_currency_code,
        )

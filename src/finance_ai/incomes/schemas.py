from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from pydantic.json_schema import WithJsonSchema

from finance_ai.accounts.schemas import format_money

MAX_INCOME_AMOUNT = Decimal("9999999999999999.99")


def _parse_income_amount(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("Use a decimal string for money values")
    return value


IncomeAmountInput = Annotated[
    Decimal,
    BeforeValidator(_parse_income_amount),
    Field(gt=0, le=MAX_INCOME_AMOUNT, max_digits=18, decimal_places=2),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^(?=.*[1-9])\d{1,16}(?:\.\d{1,2})?$",
            "examples": ["25.50"],
        }
    ),
]


@dataclass(frozen=True)
class IncomeRecord:
    id: int
    account_id: int
    account_name: str
    currency_code: str
    amount: Decimal
    date: date
    description: str | None
    income_source_id: int | None
    income_source_name: str | None
    created_at: datetime
    updated_at: datetime


class IncomeWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: Annotated[int, Field(gt=0)]
    amount: IncomeAmountInput
    date: date
    description: Annotated[str, Field(max_length=500)] | None = None
    income_source_id: Annotated[int, Field(gt=0)] | None = None

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class IncomeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    account_id: int
    account_name: str
    currency_code: str
    amount: str
    date: date
    description: str | None
    income_source_id: int | None
    income_source_name: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: IncomeRecord) -> IncomeResponse:
        return cls(
            id=record.id,
            account_id=record.account_id,
            account_name=record.account_name,
            currency_code=record.currency_code,
            amount=format_money(record.amount),
            date=record.date,
            description=record.description,
            income_source_id=record.income_source_id,
            income_source_name=record.income_source_name,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class IncomeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[IncomeResponse]

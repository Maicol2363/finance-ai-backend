"""Transfer creation DTOs for HU-010."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from pydantic.json_schema import WithJsonSchema

from finance_ai.accounts.schemas import format_money


def _parse_transfer_amount(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("Use a decimal string for money values")
    return value


TransferAmountInput = Annotated[
    Decimal,
    BeforeValidator(_parse_transfer_amount),
    Field(gt=0, max_digits=18, decimal_places=2),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^(?=.*[1-9])\d{1,16}(?:\.\d{1,2})?$",
            "examples": ["100.00"],
        }
    ),
]


@dataclass(frozen=True)
class TransferAccountSummary:
    id: int
    name: str
    currency_code: str


@dataclass(frozen=True)
class TransferRecord:
    id: int
    source_account: TransferAccountSummary
    source_amount: Decimal
    destination_account: TransferAccountSummary
    destination_amount: Decimal
    date: date
    description: str | None
    created_at: datetime
    updated_at: datetime


class TransferCreateRequest(BaseModel):
    """Manual transfer between two owned accounts.

    Both amounts are stored explicitly so cross-currency transfers never
    depend on external rates: the user declares what left and what arrived.
    """

    model_config = ConfigDict(extra="forbid")

    source_account_id: Annotated[int, Field(gt=0)]
    destination_account_id: Annotated[int, Field(gt=0)]
    source_amount: TransferAmountInput
    destination_amount: TransferAmountInput | None = None
    date: date
    description: Annotated[str, Field(max_length=500)] | None = None

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class TransferAccountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    currency_code: str


class TransferResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    source_account: TransferAccountResponse
    source_amount: str
    destination_account: TransferAccountResponse
    destination_amount: str
    date: date
    description: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: TransferRecord) -> TransferResponse:
        return cls(
            id=record.id,
            source_account=TransferAccountResponse(
                id=record.source_account.id,
                name=record.source_account.name,
                currency_code=record.source_account.currency_code,
            ),
            source_amount=format_money(record.source_amount),
            destination_account=TransferAccountResponse(
                id=record.destination_account.id,
                name=record.destination_account.name,
                currency_code=record.destination_account.currency_code,
            ),
            destination_amount=format_money(record.destination_amount),
            date=record.date,
            description=record.description,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

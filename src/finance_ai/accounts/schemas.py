from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _reject_binary_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("Use a decimal string for money values")
    return value


MoneyInput = Annotated[
    Decimal,
    BeforeValidator(_reject_binary_float),
    Field(max_digits=18, decimal_places=2),
]


class AccountType(StrEnum):
    CASH = "cash"
    BANK = "bank"
    DIGITAL_WALLET = "digital_wallet"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    OTHER = "other"


@dataclass(frozen=True)
class CurrencySummary:
    id: int
    code: str
    name: str
    decimals: int


@dataclass(frozen=True)
class AccountView:
    id: int
    name: str
    account_type: AccountType
    currency: CurrencySummary
    opening_balance: Decimal
    balance: Decimal
    currency_is_locked: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AccountListResult:
    accounts: tuple[AccountView, ...]
    suggested_currency_code: str
    supported_currencies: tuple[CurrencySummary, ...]
    account_types: tuple[AccountType, ...]


@dataclass(frozen=True)
class AccountCreateCommand:
    name: str
    account_type: AccountType
    currency_code: str
    opening_balance: Decimal


@dataclass(frozen=True)
class AccountUpdateCommand:
    name: str | None = None
    account_type: AccountType | None = None
    currency_code: str | None = None
    opening_balance: Decimal | None = None


class AccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    type: AccountType
    currency_code: str = Field(min_length=3, max_length=10)
    opening_balance: MoneyInput

    def to_command(self) -> AccountCreateCommand:
        return AccountCreateCommand(
            name=self.name,
            account_type=self.type,
            currency_code=self.currency_code,
            opening_balance=self.opening_balance,
        )


class AccountUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    type: AccountType | None = None
    currency_code: str | None = Field(default=None, min_length=3, max_length=10)
    opening_balance: MoneyInput | None = None

    @field_validator("name", "type", "currency_code", "opening_balance", mode="after")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field cannot be null")
        return value

    @model_validator(mode="after")
    def require_field(self) -> AccountUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self

    def to_command(self) -> AccountUpdateCommand:
        fields = self.model_fields_set
        return AccountUpdateCommand(
            name=self.name if "name" in fields else None,
            account_type=self.type if "type" in fields else None,
            currency_code=self.currency_code if "currency_code" in fields else None,
            opening_balance=self.opening_balance if "opening_balance" in fields else None,
        )


class AccountStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool


class CurrencyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    code: str
    name: str
    decimals: int

    @classmethod
    def from_result(cls, result: CurrencySummary) -> CurrencyResponse:
        return cls(id=result.id, code=result.code, name=result.name, decimals=result.decimals)


class AccountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    type: AccountType
    currency: CurrencyResponse
    opening_balance: str
    balance: str
    currency_is_locked: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_result(cls, result: AccountView) -> AccountResponse:
        return cls(
            id=result.id,
            name=result.name,
            type=result.account_type,
            currency=CurrencyResponse.from_result(result.currency),
            opening_balance=format_money(result.opening_balance),
            balance=format_money(result.balance),
            currency_is_locked=result.currency_is_locked,
            is_active=result.is_active,
            created_at=result.created_at,
            updated_at=result.updated_at,
        )


class AccountListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[AccountResponse]
    suggested_currency_code: str
    supported_currencies: list[CurrencyResponse]
    account_types: list[AccountType]

    @classmethod
    def from_result(cls, result: AccountListResult) -> AccountListResponse:
        return cls(
            accounts=[AccountResponse.from_result(account) for account in result.accounts],
            suggested_currency_code=result.suggested_currency_code,
            supported_currencies=[
                CurrencyResponse.from_result(currency) for currency in result.supported_currencies
            ],
            account_types=list(result.account_types),
        )


def format_money(value: Decimal) -> str:
    return format(value, ".2f")

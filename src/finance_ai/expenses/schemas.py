"""HTTP and application schemas for HU-008 and HU-011."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_ai.accounts.schemas import MoneyInput, format_money
from finance_ai.expenses.repositories import (
    CategoryReference,
    CurrencyReference,
    ExpenseItemRecord,
    ExpenseRecord,
    HistoryExpenseRecord,
    HistoryMovementRecord,
    HistoryRecord,
)


class TransactionType(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"


class HistoryPeriod(StrEnum):
    WEEK = "week"
    WEEKLY = "weekly"
    MONTH = "month"
    MONTHLY = "monthly"
    CUSTOM = "custom"


@dataclass(frozen=True)
class ExpenseUpdateCommand:
    fields: frozenset[str]
    name: str | None = None
    amount: Decimal | None = None
    note: str | None = None
    receipt_name: str | None = None
    receipt_note: str | None = None
    category_id: int | None = None
    account_id: int | None = None
    occurred_at: datetime | None = None
    description: str | None = None


@dataclass(frozen=True)
class ExpenseListResult:
    items: tuple[ExpenseRecord, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class HistoryResult:
    items: tuple[HistoryRecord, ...]
    total: int
    page: int
    page_size: int


class ExpenseUpdateRequest(BaseModel):
    """Fields that HU-008 allows a user to correct on an existing expense."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    amount: MoneyInput | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=5_000)
    receipt_name: str | None = Field(default=None, min_length=1, max_length=150)
    receipt_note: str | None = Field(default=None, max_length=5_000)
    category_id: int | None = Field(default=None, gt=0)
    account_id: int | None = Field(default=None, gt=0)
    occurred_at: datetime | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name", "note", "receipt_name", "receipt_note", "description", mode="after")
    @classmethod
    def trim_text(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()

    @field_validator("occurred_at", mode="after")
    @classmethod
    def normalize_occurred_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_fields(self) -> ExpenseUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        for field in ("name", "amount", "receipt_name", "account_id", "occurred_at"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self

    def to_command(self) -> ExpenseUpdateCommand:
        fields = frozenset(self.model_fields_set)
        return ExpenseUpdateCommand(
            fields=fields,
            name=self.name if "name" in fields else None,
            amount=self.amount if "amount" in fields else None,
            note=self.note if "note" in fields else None,
            receipt_name=self.receipt_name if "receipt_name" in fields else None,
            receipt_note=self.receipt_note if "receipt_note" in fields else None,
            category_id=self.category_id if "category_id" in fields else None,
            account_id=self.account_id if "account_id" in fields else None,
            occurred_at=self.occurred_at if "occurred_at" in fields else None,
            description=self.description if "description" in fields else None,
        )


class CurrencyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    code: str
    name: str
    decimals: int

    @classmethod
    def from_record(cls, currency: CurrencyReference) -> CurrencyResponse:
        return cls(
            id=currency.id,
            code=currency.code,
            name=currency.name,
            decimals=currency.decimals,
        )


class CategoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str

    @classmethod
    def from_record(cls, category: CategoryReference | None) -> CategoryResponse | None:
        return None if category is None else cls(id=category.id, name=category.name)


class ExpenseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    transaction_id: int
    receipt_id: int
    name: str
    amount: str
    quantity: str
    unit_price: str | None
    note: str | None
    category: CategoryResponse | None
    receipt_name: str
    receipt_note: str | None
    store: str
    description: str | None
    occurred_at: datetime
    status: str
    capture_source: str
    account_id: int
    account_name: str
    account_is_active: bool
    currency: CurrencyResponse
    currency_code: str
    created_at: datetime
    updated_at: datetime
    receipt_image: str | None
    image_path: str | None

    @classmethod
    def from_record(cls, item: ExpenseRecord) -> ExpenseResponse:
        return cls(
            id=item.id,
            transaction_id=item.transaction_id,
            receipt_id=item.receipt_id,
            name=item.name,
            amount=format_money(item.amount),
            quantity=format(item.quantity, ".3f"),
            unit_price=None if item.unit_price is None else format_money(item.unit_price),
            note=item.note,
            category=CategoryResponse.from_record(item.category),
            receipt_name=item.receipt_name,
            receipt_note=item.receipt_note,
            store=item.store,
            description=item.description,
            occurred_at=item.occurred_at,
            status=item.status,
            capture_source=item.capture_source,
            account_id=item.account_id,
            account_name=item.account_name,
            account_is_active=item.account_is_active,
            currency=CurrencyResponse.from_record(item.currency),
            currency_code=item.currency.code,
            created_at=item.created_at,
            updated_at=item.updated_at,
            receipt_image=item.receipt_image,
            image_path=item.image_path,
        )


class ExpenseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExpenseResponse]
    total: int
    page: int
    page_size: int
    has_next: bool


class ExpenseItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    amount: str
    quantity: str
    unit_price: str | None
    note: str | None
    category: CategoryResponse | None
    image_path: str | None

    @classmethod
    def from_record(cls, item: ExpenseItemRecord) -> ExpenseItemResponse:
        return cls(
            id=item.id,
            name=item.name,
            amount=format_money(item.amount),
            quantity=format(item.quantity, ".3f"),
            unit_price=None if item.unit_price is None else format_money(item.unit_price),
            note=item.note,
            category=CategoryResponse.from_record(item.category),
            image_path=item.image_path,
        )


class HistoryMovementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int
    account_name: str
    amount: str
    currency: CurrencyResponse
    currency_code: str

    @classmethod
    def from_record(cls, item: HistoryMovementRecord) -> HistoryMovementResponse:
        return cls(
            account_id=item.account_id,
            account_name=item.account_name,
            amount=format_money(item.amount),
            currency=CurrencyResponse.from_record(item.currency),
            currency_code=item.currency.code,
        )


class HistoryExpenseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: int
    name: str
    occurred_at: datetime
    purchased_at: datetime
    total_amount: str
    note: str | None
    store: str
    store_name: str
    items: list[ExpenseItemResponse]
    receipt_image: str | None

    @classmethod
    def from_record(cls, item: HistoryExpenseRecord) -> HistoryExpenseResponse:
        return cls(
            receipt_id=item.receipt_id,
            name=item.name,
            occurred_at=item.occurred_at,
            purchased_at=item.occurred_at,
            total_amount=format_money(item.total_amount),
            note=item.note,
            store=item.store,
            store_name=item.store,
            items=[ExpenseItemResponse.from_record(child) for child in item.items],
            receipt_image=item.receipt_image,
        )


class HistoryItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    type: TransactionType
    status: str
    occurred_at: datetime
    description: str | None
    capture_source: str
    income_source: str | None
    movements: list[HistoryMovementResponse]
    expense: HistoryExpenseResponse | None

    @classmethod
    def from_record(cls, item: HistoryRecord) -> HistoryItemResponse:
        return cls(
            id=item.id,
            type=TransactionType(item.transaction_type),
            status=item.status,
            occurred_at=item.occurred_at,
            description=item.description,
            capture_source=item.capture_source,
            income_source=item.income_source_name,
            movements=[
                HistoryMovementResponse.from_record(movement) for movement in item.movements
            ],
            expense=None
            if item.expense is None
            else HistoryExpenseResponse.from_record(item.expense),
        )


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[HistoryItemResponse]
    total: int
    page: int
    page_size: int
    has_next: bool

    @classmethod
    def from_result(cls, result: HistoryResult) -> HistoryResponse:
        return cls(
            items=[HistoryItemResponse.from_record(item) for item in result.items],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            has_next=result.page * result.page_size < result.total,
        )

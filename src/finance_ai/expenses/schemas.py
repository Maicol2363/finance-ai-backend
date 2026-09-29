"""HTTP and application schemas for HU-006, HU-008, and HU-011."""

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
    ExpenseItemInput,
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
class CreateExpenseCommand:
    account_id: int
    amount: Decimal
    merchant: str
    occurred_at: datetime
    category_id: int | None = None
    note: str | None = None


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
    items: tuple[ExpenseItemInput, ...] | None = None


@dataclass(frozen=True)
class ExpenseListResult:
    items: tuple[ExpenseRecord, ...]
    total: int
    page: int
    page_size: int

    @property
    def has_next(self) -> bool:
        return (self.page * self.page_size) < self.total


@dataclass(frozen=True)
class HistoryResult:
    items: tuple[HistoryRecord, ...]
    total: int
    page: int
    page_size: int

    @property
    def has_next(self) -> bool:
        return (self.page * self.page_size) < self.total


class ExpenseCreateRequest(BaseModel):
    """Fields required to capture a manual expense (HU-006)."""

    model_config = ConfigDict(extra="forbid")

    account_id: int = Field(gt=0)
    amount: MoneyInput = Field(gt=0)
    merchant: str = Field(min_length=1, max_length=150)
    occurred_at: datetime | None = None
    category_id: int | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=5_000)

    @field_validator("merchant", "note", mode="after")
    @classmethod
    def trim_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed if trimmed else None

    @field_validator("merchant", mode="after")
    @classmethod
    def require_non_empty_merchant(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Merchant cannot be empty")
        return value

    @field_validator("occurred_at", mode="after")
    @classmethod
    def normalize_occurred_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    def to_command(self) -> CreateExpenseCommand:
        return CreateExpenseCommand(
            account_id=self.account_id,
            amount=self.amount,
            merchant=self.merchant,
            occurred_at=self.occurred_at if self.occurred_at is not None else datetime.now(UTC),
            category_id=self.category_id,
            note=self.note,
        )


class ExpenseItemUpdateRequest(BaseModel):
    """One line of a receipt expense in an update payload.

    Field limits mirror ``receipts.schemas.ConfirmedReceiptItem`` so capture-time
    confirmation and post-hoc editing accept the same shape.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    total_price: Decimal = Field(gt=Decimal("0.00"), le=Decimal("9999999999.99"))
    quantity: Decimal = Field(default=Decimal("1.0"), gt=Decimal("0.00"), le=Decimal("999999.999"))
    unit_price: Decimal | None = Field(
        default=None, ge=Decimal("0.00"), le=Decimal("9999999999.99")
    )
    category_id: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("name", mode="after")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Item name cannot be empty")
        return cleaned

    @field_validator("note", mode="after")
    @classmethod
    def clean_note(cls, value: str | None) -> str | None:
        return None if value is None or not value.strip() else value

    def to_input(self) -> ExpenseItemInput:
        return ExpenseItemInput(
            name=self.name,
            total_price=self.total_price,
            quantity=self.quantity,
            unit_price=self.unit_price,
            category_id=self.category_id,
            note=self.note,
        )


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
    items: list[ExpenseItemUpdateRequest] | None = Field(default=None, max_length=500)

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
        provided = self.model_fields_set - ({"items"} if self.items is None else set())
        if not provided:
            raise ValueError("At least one field is required")
        for field in ("name", "amount", "receipt_name", "account_id", "occurred_at"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self

    def to_command(self) -> ExpenseUpdateCommand:
        fields = frozenset(self.model_fields_set)
        items = self.items
        # An explicit null means "leave the receipt lines untouched".
        fields = fields - {"items"} if items is None else fields | {"items"}
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
            items=None if items is None else tuple(item.to_input() for item in items),
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


class ExpenseItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    amount: str
    quantity: str
    unit_price: str | None
    note: str | None
    category: CategoryResponse | None

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
        )


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
    receipt_total: str | None = None
    items: list[ExpenseItemResponse] = Field(default_factory=list)

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
            receipt_total=format_money(item.receipt_total)
            if item.receipt_total is not None
            else None,
            items=[ExpenseItemResponse.from_record(sub) for sub in item.items],
        )


class ExpenseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExpenseResponse]
    total: int
    page: int
    page_size: int
    has_next: bool


class HistoryExpenseItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    amount: str
    quantity: str
    unit_price: str | None
    note: str | None
    category: CategoryResponse | None
    image_path: str | None = None

    @classmethod
    def from_record(cls, item: ExpenseItemRecord) -> HistoryExpenseItemResponse:
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
    items: list[HistoryExpenseItemResponse]
    receipt_image: str | None

    @classmethod
    def from_record(cls, expense: HistoryExpenseRecord) -> HistoryExpenseResponse:
        return cls(
            receipt_id=expense.receipt_id,
            name=expense.name,
            occurred_at=expense.occurred_at,
            purchased_at=expense.occurred_at,
            total_amount=format_money(expense.total_amount),
            note=expense.note,
            store=expense.store,
            store_name=expense.store,
            items=[HistoryExpenseItemResponse.from_record(item) for item in expense.items],
            receipt_image=expense.receipt_image,
        )


class HistoryMovementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int
    account_name: str
    amount: str
    currency: CurrencyResponse
    currency_code: str

    @classmethod
    def from_record(cls, movement: HistoryMovementRecord) -> HistoryMovementResponse:
        return cls(
            account_id=movement.account_id,
            account_name=movement.account_name,
            amount=format_money(movement.amount),
            currency=CurrencyResponse.from_record(movement.currency),
            currency_code=movement.currency.code,
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
    def from_record(cls, record: HistoryRecord) -> HistoryItemResponse:
        return cls(
            id=record.id,
            type=TransactionType(record.transaction_type),
            status=record.status,
            occurred_at=record.occurred_at,
            description=record.description,
            capture_source=record.capture_source,
            income_source=record.income_source_name,
            movements=[HistoryMovementResponse.from_record(m) for m in record.movements],
            expense=(
                None
                if record.expense is None
                else HistoryExpenseResponse.from_record(record.expense)
            ),
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
            has_next=result.has_next,
        )

"""Data transfer records and persistence contract for HU-006, HU-008, and HU-011."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from finance_ai.receipts.schemas import ConfirmedReceiptItem


@dataclass(frozen=True)
class CurrencyReference:
    id: int
    code: str
    name: str
    decimals: int


@dataclass(frozen=True)
class CategoryReference:
    id: int
    name: str


@dataclass(frozen=True)
class ExpenseRecord:
    id: int
    transaction_id: int
    receipt_id: int
    name: str
    amount: Decimal
    quantity: Decimal
    unit_price: Decimal | None
    note: str | None
    category: CategoryReference | None
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
    currency: CurrencyReference
    created_at: datetime
    updated_at: datetime
    receipt_image: str | None = None
    image_path: str | None = None
    receipt_total: Decimal | None = None
    items: tuple[ExpenseItemRecord, ...] = ()


@dataclass(frozen=True)
class ExpenseItemRecord:
    id: int
    name: str
    amount: Decimal
    quantity: Decimal
    unit_price: Decimal | None
    note: str | None
    category: CategoryReference | None
    image_path: str | None = None


@dataclass(frozen=True)
class HistoryExpenseRecord:
    receipt_id: int
    name: str
    occurred_at: datetime
    total_amount: Decimal
    note: str | None
    store: str
    items: tuple[ExpenseItemRecord, ...]
    receipt_image: str | None = None


@dataclass(frozen=True)
class HistoryMovementRecord:
    account_id: int
    account_name: str
    amount: Decimal
    currency: CurrencyReference


@dataclass(frozen=True)
class HistoryRecord:
    id: int
    transaction_type: str
    status: str
    occurred_at: datetime
    description: str | None
    capture_source: str
    income_source_name: str | None
    movements: tuple[HistoryMovementRecord, ...]
    expense: HistoryExpenseRecord | None


@dataclass(frozen=True)
class HistoryFilters:
    search: str | None = None
    transaction_type: str | None = None
    category_id: int | None = None
    account_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    offset: int = 0
    limit: int = 50


class ExpenseRepository(Protocol):
    def list_owned(
        self, *, owner_id: int, offset: int, limit: int
    ) -> tuple[list[ExpenseRecord], int]: ...

    def find_owned(
        self, *, owner_id: int, expense_id: int, for_update: bool = False
    ) -> ExpenseRecord | None: ...

    def find_by_transaction_id(
        self, *, owner_id: int, transaction_id: int
    ) -> ExpenseRecord | None: ...

    def active_account_currency(
        self, *, owner_id: int, account_id: int
    ) -> CurrencyReference | None: ...

    def category_owned(self, *, owner_id: int, category_id: int) -> bool: ...

    def create_manual(
        self,
        *,
        owner_id: int,
        account_id: int,
        amount: Decimal,
        merchant: str,
        occurred_at: datetime,
        category_id: int | None = None,
        note: str | None = None,
    ) -> ExpenseRecord: ...

    def update(
        self,
        *,
        owner_id: int,
        expense_id: int,
        fields: frozenset[str],
        name: str | None,
        amount: Decimal | None,
        note: str | None,
        receipt_name: str | None,
        receipt_note: str | None,
        category_id: int | None,
        account_id: int | None,
        occurred_at: datetime | None,
        description: str | None,
    ) -> None: ...

    def delete(self, *, owner_id: int, expense_id: int) -> None: ...

    def list_history(
        self, *, owner_id: int, filters: HistoryFilters
    ) -> tuple[list[HistoryRecord], int]: ...

    def create_from_receipt_proposal(
        self,
        *,
        owner_id: int,
        account_id: int,
        total_amount: Decimal,
        merchant: str,
        occurred_at: datetime,
        category_id: int | None = None,
        items: Sequence[ConfirmedReceiptItem] | None = None,
        note: str | None = None,
    ) -> ExpenseRecord: ...

    def find_potential_duplicate(
        self,
        *,
        owner_id: int,
        merchant: str,
        total_amount: Decimal,
        occurred_date: date,
    ) -> bool: ...

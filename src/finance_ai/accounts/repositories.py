from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from finance_ai.accounts.schemas import AccountType, CurrencySummary


class ActiveAccountNameConstraintError(Exception):
    """Raised only when the authoritative active-name constraint rejects a write."""


class AccountMovementConstraintError(Exception):
    """Raised when persistence prevents deleting an account with movement history."""


@dataclass(frozen=True)
class AccountRecord:
    id: int
    user_id: int
    name: str
    account_type: AccountType
    currency: CurrencySummary
    opening_balance: Decimal
    balance: Decimal
    currency_is_locked: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AccountRepository(Protocol):
    def list_owned(self, *, owner_id: int, include_inactive: bool) -> list[AccountRecord]: ...

    def find_owned(
        self, *, owner_id: int, account_id: int, for_update: bool = False
    ) -> AccountRecord | None: ...

    def find_active_owned(self, *, owner_id: int, account_id: int) -> AccountRecord | None: ...

    def active_name_exists(
        self, *, owner_id: int, normalized_name: str, excluding_id: int | None = None
    ) -> bool: ...

    def add(
        self,
        *,
        owner_id: int,
        name: str,
        account_type: AccountType,
        currency_id: int,
        opening_balance: Decimal,
    ) -> int: ...

    def update_metadata(
        self,
        *,
        owner_id: int,
        account_id: int,
        name: str | None,
        account_type: AccountType | None,
        currency_id: int | None,
        opening_balance: Decimal | None,
    ) -> None: ...

    def set_active(self, *, owner_id: int, account_id: int, is_active: bool) -> None: ...

    def has_confirmed_movements(self, *, owner_id: int, account_id: int) -> bool: ...

    def has_movements(self, *, owner_id: int, account_id: int) -> bool: ...

    def delete(self, *, owner_id: int, account_id: int) -> None: ...

    def latest_active_currency_code(self, *, owner_id: int) -> str | None: ...


class CurrencyRepository(Protocol):
    def list_active(self) -> list[CurrencySummary]: ...

    def find_active_by_code(self, code: str) -> CurrencySummary | None: ...

    def first_active_code(self, codes: tuple[str, ...]) -> str | None: ...

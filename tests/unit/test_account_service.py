from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest

from finance_ai.accounts.policy import AccountPolicy
from finance_ai.accounts.repositories import (
    AccountMovementConstraintError,
    AccountRecord,
    ActiveAccountNameConstraintError,
)
from finance_ai.accounts.schemas import (
    AccountCreateCommand,
    AccountType,
    AccountUpdateCommand,
    CurrencySummary,
)
from finance_ai.accounts.service import AccountService
from finance_ai.core.errors import (
    AccountCurrencyInvalidError,
    AccountCurrencyLockedError,
    AccountDeleteBlockedError,
    AccountNameConflictError,
    AccountNotFoundError,
    AccountReactivationConflictError,
)

NOW = datetime(2026, 9, 7, tzinfo=UTC)
COP = CurrencySummary(id=1, code="COP", name="Colombian Peso", decimals=2)
USD = CurrencySummary(id=2, code="USD", name="US Dollar", decimals=2)


@dataclass
class Store:
    accounts: dict[int, AccountRecord] = field(default_factory=dict)
    currencies: dict[str, CurrencySummary] = field(default_factory=lambda: {"COP": COP, "USD": USD})
    confirmed_accounts: set[int] = field(default_factory=set)
    movement_accounts: set[int] = field(default_factory=set)
    next_id: int = 1
    commits: int = 0
    rollbacks: int = 0
    flushes: int = 0
    race_on_write: bool = False
    movement_race_on_delete: bool = False


class FakeAccounts:
    def __init__(self, store: Store) -> None:
        self.store = store

    def list_owned(self, *, owner_id: int, include_inactive: bool) -> list[AccountRecord]:
        values = [
            replace(
                account,
                currency_is_locked=account.id in self.store.confirmed_accounts,
            )
            for account in self.store.accounts.values()
            if account.user_id == owner_id and (include_inactive or account.is_active)
        ]
        return sorted(values, key=lambda item: (not item.is_active, item.name.lower(), item.id))

    def find_owned(
        self, *, owner_id: int, account_id: int, for_update: bool = False
    ) -> AccountRecord | None:
        del for_update
        account = self.store.accounts.get(account_id)
        if account is None or account.user_id != owner_id:
            return None
        return replace(
            account,
            currency_is_locked=account.id in self.store.confirmed_accounts,
        )

    def find_active_owned(self, *, owner_id: int, account_id: int) -> AccountRecord | None:
        account = self.find_owned(owner_id=owner_id, account_id=account_id)
        return account if account is not None and account.is_active else None

    def active_name_exists(
        self, *, owner_id: int, normalized_name: str, excluding_id: int | None = None
    ) -> bool:
        return any(
            item.user_id == owner_id
            and item.is_active
            and item.id != excluding_id
            and item.name.lower() == normalized_name.lower()
            for item in self.store.accounts.values()
        )

    def add(
        self,
        *,
        owner_id: int,
        name: str,
        account_type: AccountType,
        currency_id: int,
        opening_balance: Decimal,
    ) -> int:
        if self.store.race_on_write:
            raise ActiveAccountNameConstraintError
        currency = next(item for item in self.store.currencies.values() if item.id == currency_id)
        account_id = self.store.next_id
        self.store.next_id += 1
        self.store.accounts[account_id] = AccountRecord(
            id=account_id,
            user_id=owner_id,
            name=name,
            account_type=account_type,
            currency=currency,
            opening_balance=opening_balance,
            balance=opening_balance,
            currency_is_locked=False,
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        return account_id

    def update_metadata(
        self,
        *,
        owner_id: int,
        account_id: int,
        name: str | None,
        account_type: AccountType | None,
        currency_id: int | None,
        opening_balance: Decimal | None,
    ) -> None:
        if self.store.race_on_write:
            raise ActiveAccountNameConstraintError
        current = self.store.accounts[account_id]
        assert current.user_id == owner_id
        currency = current.currency
        if currency_id is not None:
            currency = next(
                item for item in self.store.currencies.values() if item.id == currency_id
            )
        next_opening = opening_balance if opening_balance is not None else current.opening_balance
        movement_total = current.balance - current.opening_balance
        self.store.accounts[account_id] = replace(
            current,
            name=name if name is not None else current.name,
            account_type=account_type if account_type is not None else current.account_type,
            currency=currency,
            opening_balance=next_opening,
            balance=next_opening + movement_total,
            updated_at=NOW,
        )

    def set_active(self, *, owner_id: int, account_id: int, is_active: bool) -> None:
        if self.store.race_on_write:
            raise ActiveAccountNameConstraintError
        current = self.store.accounts[account_id]
        assert current.user_id == owner_id
        self.store.accounts[account_id] = replace(current, is_active=is_active, updated_at=NOW)

    def has_confirmed_movements(self, *, owner_id: int, account_id: int) -> bool:
        account = self.store.accounts.get(account_id)
        return (
            account is not None
            and account.user_id == owner_id
            and account_id in self.store.confirmed_accounts
        )

    def has_movements(self, *, owner_id: int, account_id: int) -> bool:
        account = self.store.accounts.get(account_id)
        return (
            account is not None
            and account.user_id == owner_id
            and (
                account_id in self.store.confirmed_accounts
                or account_id in self.store.movement_accounts
            )
        )

    def delete(self, *, owner_id: int, account_id: int) -> None:
        if self.store.movement_race_on_delete:
            raise AccountMovementConstraintError
        account = self.store.accounts[account_id]
        assert account.user_id == owner_id
        del self.store.accounts[account_id]

    def latest_active_currency_code(self, *, owner_id: int) -> str | None:
        candidates = [
            item
            for item in self.store.accounts.values()
            if item.user_id == owner_id and item.currency.code in self.store.currencies
        ]
        return max(candidates, key=lambda item: item.id).currency.code if candidates else None


class FakeCurrencies:
    def __init__(self, store: Store) -> None:
        self.store = store

    def list_active(self) -> list[CurrencySummary]:
        return sorted(self.store.currencies.values(), key=lambda item: item.code)

    def find_active_by_code(self, code: str) -> CurrencySummary | None:
        return self.store.currencies.get(code)

    def first_active_code(self, codes: tuple[str, ...]) -> str | None:
        return next((code for code in codes if code in self.store.currencies), None)


class FakeUow:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.accounts = FakeAccounts(store)
        self.currencies = FakeCurrencies(store)
        self.committed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        if exc_type is not None or not self.committed:
            self.store.rollbacks += 1

    def flush(self) -> None:
        self.store.flushes += 1

    def commit(self) -> None:
        self.store.commits += 1
        self.committed = True

    def rollback(self) -> None:
        self.store.rollbacks += 1


def make_service(store: Store | None = None) -> tuple[AccountService, Store]:
    target = store or Store()
    service = AccountService(
        policy=AccountPolicy(),
        uow_factory=lambda: FakeUow(target),  # type: ignore[arg-type]
    )
    return service, target


def create(
    service: AccountService,
    *,
    owner_id: int = 7,
    name: str = "Cash",
    currency: str = "COP",
    opening: str = "100.00",
) -> object:
    return service.create(
        owner_id=owner_id,
        command=AccountCreateCommand(
            name=name,
            account_type=AccountType.CASH,
            currency_code=currency,
            opening_balance=Decimal(opening),
        ),
    )


def test_create_normalizes_and_commits_exact_signed_opening_balance() -> None:
    service, store = make_service()

    result = create(service, name="  Daily cash  ", currency="cop", opening="-12.50")

    assert result.name == "Daily cash"  # type: ignore[attr-defined]
    assert result.opening_balance == Decimal("-12.50")  # type: ignore[attr-defined]
    assert result.balance == Decimal("-12.50")  # type: ignore[attr-defined]
    assert store.commits == 1
    assert store.flushes == 1
    assert store.rollbacks == 0


def test_friendly_and_raced_duplicate_names_share_the_safe_conflict() -> None:
    service, store = make_service()
    create(service)

    with pytest.raises(AccountNameConflictError) as friendly:
        create(service, name="cash")
    store.race_on_write = True
    with pytest.raises(AccountNameConflictError) as raced:
        create(service, name="Wallet")

    assert raced.value.code == friendly.value.code == "account_name_conflict"
    assert "Cash" not in raced.value.detail


def test_list_defaults_to_active_and_uses_latest_currency_suggestion() -> None:
    service, _ = make_service()
    first = create(service, name="Zulu", currency="COP")
    second = create(service, name="Alpha", currency="USD")
    service.set_active(owner_id=7, account_id=first.id, is_active=False)  # type: ignore[attr-defined]

    active = service.list(owner_id=7, preferred_currency_code="COP", include_inactive=False)
    all_accounts = service.list(owner_id=7, preferred_currency_code="COP", include_inactive=True)

    assert [item.name for item in active.accounts] == ["Alpha"]
    assert [item.name for item in all_accounts.accounts] == ["Alpha", "Zulu"]
    assert active.suggested_currency_code == second.currency.code == "USD"  # type: ignore[attr-defined]
    assert active.account_types == tuple(AccountType)


def test_missing_and_foreign_accounts_share_the_same_not_found_contract() -> None:
    service, _ = make_service()
    account = create(service, owner_id=8)

    with pytest.raises(AccountNotFoundError) as foreign:
        service.get(owner_id=7, account_id=account.id)  # type: ignore[attr-defined]
    with pytest.raises(AccountNotFoundError) as missing:
        service.get(owner_id=7, account_id=999)

    assert foreign.value.code == missing.value.code == "account_not_found"
    assert foreign.value.detail == missing.value.detail


def test_confirmed_movement_locks_only_currency_and_opening_correction_updates_balance() -> None:
    service, store = make_service()
    account = create(service)
    account_id = account.id  # type: ignore[attr-defined]
    store.accounts[account_id] = replace(store.accounts[account_id], balance=Decimal("80.00"))
    store.confirmed_accounts.add(account_id)

    with pytest.raises(AccountCurrencyLockedError):
        service.update(
            owner_id=7,
            account_id=account_id,
            command=AccountUpdateCommand(currency_code="USD"),
        )
    updated = service.update(
        owner_id=7,
        account_id=account_id,
        command=AccountUpdateCommand(name="Pocket", opening_balance=Decimal("150.00")),
    )

    assert updated.name == "Pocket"
    assert updated.opening_balance == Decimal("150.00")
    assert updated.balance == Decimal("130.00")
    assert updated.currency_is_locked is True


def test_inactive_name_can_be_reused_but_conflict_blocks_reactivation() -> None:
    service, _ = make_service()
    old = create(service, name="Savings")
    service.set_active(owner_id=7, account_id=old.id, is_active=False)  # type: ignore[attr-defined]
    create(service, name="savings")

    with pytest.raises(AccountReactivationConflictError):
        service.set_active(owner_id=7, account_id=old.id, is_active=True)  # type: ignore[attr-defined]


def test_repeating_current_status_is_idempotent_without_commit() -> None:
    service, store = make_service()
    account = create(service)
    commits = store.commits

    result = service.set_active(owner_id=7, account_id=account.id, is_active=True)  # type: ignore[attr-defined]

    assert result.is_active is True
    assert store.commits == commits


def test_unsupported_currency_fails_without_committing() -> None:
    service, store = make_service()

    with pytest.raises(AccountCurrencyInvalidError):
        create(service, currency="EUR")

    assert store.commits == 0
    assert store.rollbacks == 1


def test_delete_unused_account_with_opening_balance_is_permanent() -> None:
    service, store = make_service()
    account = create(service, opening="975.25")
    account_id = account.id  # type: ignore[attr-defined]

    service.delete(owner_id=7, account_id=account_id)

    assert account_id not in store.accounts
    assert store.commits == 2
    assert store.flushes == 2


def test_delete_blocks_every_movement_status_and_rolls_back() -> None:
    service, store = make_service()
    account = create(service)
    account_id = account.id  # type: ignore[attr-defined]
    store.movement_accounts.add(account_id)

    with pytest.raises(AccountDeleteBlockedError) as failure:
        service.delete(owner_id=7, account_id=account_id)

    assert failure.value.code == "account_delete_blocked"
    assert account_id in store.accounts
    assert store.commits == 1
    assert store.rollbacks == 1


def test_delete_missing_and_foreign_accounts_share_not_found() -> None:
    service, _ = make_service()
    account = create(service, owner_id=8)

    with pytest.raises(AccountNotFoundError) as foreign:
        service.delete(owner_id=7, account_id=account.id)  # type: ignore[attr-defined]
    with pytest.raises(AccountNotFoundError) as missing:
        service.delete(owner_id=7, account_id=999)

    assert foreign.value.code == missing.value.code == "account_not_found"


def test_delete_constraint_race_maps_to_blocked_and_rolls_back() -> None:
    service, store = make_service()
    account = create(service)
    account_id = account.id  # type: ignore[attr-defined]
    store.movement_race_on_delete = True

    with pytest.raises(AccountDeleteBlockedError):
        service.delete(owner_id=7, account_id=account_id)

    assert account_id in store.accounts
    assert store.commits == 1
    assert store.rollbacks == 1

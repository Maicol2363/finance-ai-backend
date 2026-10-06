from collections.abc import Callable

from finance_ai.accounts.policy import AccountPolicy
from finance_ai.accounts.repositories import (
    AccountMovementConstraintError,
    AccountRecord,
    ActiveAccountNameConstraintError,
)
from finance_ai.accounts.schemas import (
    AccountCreateCommand,
    AccountListResult,
    AccountType,
    AccountUpdateCommand,
    AccountView,
)
from finance_ai.accounts.uow import AccountUnitOfWork
from finance_ai.core.errors import (
    AccountCurrencyInvalidError,
    AccountCurrencyLockedError,
    AccountDeleteBlockedError,
    AccountNameConflictError,
    AccountNotFoundError,
    AccountReactivationConflictError,
    ServiceUnavailableError,
)

AccountUnitOfWorkFactory = Callable[[], AccountUnitOfWork]


class AccountService:
    def __init__(self, *, policy: AccountPolicy, uow_factory: AccountUnitOfWorkFactory) -> None:
        self.policy = policy
        self.uow_factory = uow_factory

    def create(self, *, owner_id: int, command: AccountCreateCommand) -> AccountView:
        name = self.policy.normalize_name(command.name)
        account_type = self.policy.validate_account_type(command.account_type)
        currency_code = self.policy.normalize_currency_code(command.currency_code)
        opening_balance = self.policy.validate_opening_balance(command.opening_balance)

        with self.uow_factory() as uow:
            currency = uow.currencies.find_active_by_code(currency_code)
            if currency is None:
                raise AccountCurrencyInvalidError
            if uow.accounts.active_name_exists(owner_id=owner_id, normalized_name=name):
                raise AccountNameConflictError
            try:
                account_id = uow.accounts.add(
                    owner_id=owner_id,
                    name=name,
                    account_type=account_type,
                    currency_id=currency.id,
                    opening_balance=opening_balance,
                )
                uow.flush()
            except ActiveAccountNameConstraintError:
                raise AccountNameConflictError from None
            record = self._require_owned(uow, owner_id=owner_id, account_id=account_id)
            uow.commit()
            return self._view(record)

    def list(
        self, *, owner_id: int, preferred_currency_code: str, include_inactive: bool
    ) -> AccountListResult:
        with self.uow_factory() as uow:
            accounts = uow.accounts.list_owned(
                owner_id=owner_id,
                include_inactive=include_inactive,
            )
            supported_currencies = uow.currencies.list_active()
            suggested_currency = uow.accounts.latest_active_currency_code(owner_id=owner_id)
            if suggested_currency is None:
                suggested_currency = uow.currencies.first_active_code(
                    (preferred_currency_code.upper(), "COP")
                )
            if suggested_currency is None:
                raise ServiceUnavailableError
        return AccountListResult(
            accounts=tuple(self._view(account) for account in accounts),
            suggested_currency_code=suggested_currency,
            supported_currencies=tuple(supported_currencies),
            account_types=tuple(AccountType),
        )

    def get(self, *, owner_id: int, account_id: int) -> AccountView:
        with self.uow_factory() as uow:
            return self._view(self._require_owned(uow, owner_id=owner_id, account_id=account_id))

    def update(
        self, *, owner_id: int, account_id: int, command: AccountUpdateCommand
    ) -> AccountView:
        self.policy.validate_update(command)
        name = self.policy.normalize_name(command.name) if command.name is not None else None
        account_type = (
            self.policy.validate_account_type(command.account_type)
            if command.account_type is not None
            else None
        )
        currency_code = (
            self.policy.normalize_currency_code(command.currency_code)
            if command.currency_code is not None
            else None
        )
        opening_balance = (
            self.policy.validate_opening_balance(command.opening_balance)
            if command.opening_balance is not None
            else None
        )

        with self.uow_factory() as uow:
            current = self._require_owned(
                uow,
                owner_id=owner_id,
                account_id=account_id,
                for_update=True,
            )
            currency_id: int | None = None
            if currency_code is not None:
                currency = uow.currencies.find_active_by_code(currency_code)
                if currency is None:
                    raise AccountCurrencyInvalidError
                if currency.code != current.currency.code and uow.accounts.has_confirmed_movements(
                    owner_id=owner_id, account_id=account_id
                ):
                    raise AccountCurrencyLockedError
                currency_id = currency.id
            if (
                current.is_active
                and name is not None
                and uow.accounts.active_name_exists(
                    owner_id=owner_id,
                    normalized_name=name,
                    excluding_id=account_id,
                )
            ):
                raise AccountNameConflictError
            try:
                uow.accounts.update_metadata(
                    owner_id=owner_id,
                    account_id=account_id,
                    name=name,
                    account_type=account_type,
                    currency_id=currency_id,
                    opening_balance=opening_balance,
                )
                uow.flush()
            except ActiveAccountNameConstraintError:
                raise AccountNameConflictError from None
            updated = self._require_owned(uow, owner_id=owner_id, account_id=account_id)
            uow.commit()
            return self._view(updated)

    def set_active(self, *, owner_id: int, account_id: int, is_active: bool) -> AccountView:
        with self.uow_factory() as uow:
            current = self._require_owned(
                uow,
                owner_id=owner_id,
                account_id=account_id,
                for_update=True,
            )
            if current.is_active == is_active:
                return self._view(current)
            if is_active:
                currency = uow.currencies.find_active_by_code(current.currency.code)
                name_conflict = uow.accounts.active_name_exists(
                    owner_id=owner_id,
                    normalized_name=current.name,
                    excluding_id=account_id,
                )
                if currency is None or name_conflict:
                    raise AccountReactivationConflictError
            try:
                uow.accounts.set_active(
                    owner_id=owner_id,
                    account_id=account_id,
                    is_active=is_active,
                )
                uow.flush()
            except ActiveAccountNameConstraintError:
                raise AccountReactivationConflictError from None
            updated = self._require_owned(uow, owner_id=owner_id, account_id=account_id)
            uow.commit()
            return self._view(updated)

    def delete(self, *, owner_id: int, account_id: int) -> None:
        with self.uow_factory() as uow:
            self._require_owned(
                uow,
                owner_id=owner_id,
                account_id=account_id,
                for_update=True,
            )
            if uow.accounts.has_movements(owner_id=owner_id, account_id=account_id):
                raise AccountDeleteBlockedError
            try:
                uow.accounts.delete(owner_id=owner_id, account_id=account_id)
                uow.flush()
            except AccountMovementConstraintError:
                raise AccountDeleteBlockedError from None
            uow.commit()

    @staticmethod
    def _require_owned(
        uow: AccountUnitOfWork,
        *,
        owner_id: int,
        account_id: int,
        for_update: bool = False,
    ) -> AccountRecord:
        account = uow.accounts.find_owned(
            owner_id=owner_id,
            account_id=account_id,
            for_update=for_update,
        )
        if account is None:
            raise AccountNotFoundError
        return account

    @staticmethod
    def _view(record: AccountRecord) -> AccountView:
        return AccountView(
            id=record.id,
            name=record.name,
            account_type=record.account_type,
            currency=record.currency,
            opening_balance=record.opening_balance,
            balance=record.balance,
            currency_is_locked=record.currency_is_locked,
            is_active=record.is_active,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

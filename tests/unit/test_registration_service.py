from types import TracebackType
from typing import Self

import pytest
from sqlalchemy.exc import IntegrityError

from finance_ai.auth.policy import RegistrationPolicy
from finance_ai.auth.repositories import CurrencyRecord, UserRecord
from finance_ai.auth.schemas import RegisterCommand
from finance_ai.auth.service import RegistrationService
from finance_ai.core.errors import EmailAlreadyRegisteredError
from finance_ai.db.seeds import REGISTRATION_CATALOG_TEMPLATE, RegistrationCatalogTemplate


class FakeUnsafePasswords:
    def __init__(self) -> None:
        self.checked: list[str] = []

    def reject_if_unsafe(self, candidate: str) -> None:
        self.checked.append(candidate)


class FakeHasher:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def hash(self, password: str) -> str:
        self.seen.append(password)
        return "$argon2id$test-hash"


class FakeCurrencies:
    def require_active_by_code(self, code: str) -> CurrencyRecord:
        assert code == "COP"
        return CurrencyRecord(id=7, code="COP")


class FakeUsers:
    def __init__(self, *, exists: bool = False, add_error: Exception | None = None) -> None:
        self.exists = exists
        self.add_error = add_error
        self.added: list[dict[str, object]] = []

    def exists_by_email(self, normalized_email: str) -> bool:
        assert normalized_email == "person@example.com"
        return self.exists

    def add(self, *, email: str, password_hash: str, preferred_currency_id: int) -> UserRecord:
        if self.add_error is not None:
            raise self.add_error
        self.added.append(
            {
                "email": email,
                "password_hash": password_hash,
                "preferred_currency_id": preferred_currency_id,
            }
        )
        return UserRecord(id=42, email=email)


class FakeCatalogs:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[int, RegistrationCatalogTemplate]] = []

    def add_defaults(self, *, user_id: int, template: RegistrationCatalogTemplate) -> None:
        if self.fail:
            raise RuntimeError("staging failed")
        self.calls.append((user_id, template))


class FakeUnitOfWork:
    def __init__(
        self,
        *,
        exists: bool = False,
        catalog_failure: bool = False,
        add_error: Exception | None = None,
    ) -> None:
        self.currencies = FakeCurrencies()
        self.users = FakeUsers(exists=exists, add_error=add_error)
        self.catalogs = FakeCatalogs(fail=catalog_failure)
        self.flushes = 0
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None or self.commits == 0:
            self.rollbacks += 1

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def registration_service(
    uow: FakeUnitOfWork,
) -> tuple[RegistrationService, FakeUnsafePasswords, FakeHasher]:
    unsafe = FakeUnsafePasswords()
    hasher = FakeHasher()
    service = RegistrationService(  # type: ignore[arg-type]
        policy=RegistrationPolicy(),
        unsafe_passwords=unsafe,
        password_hasher=hasher,
        uow_factory=lambda: uow,
    )
    return service, unsafe, hasher


def valid_command() -> RegisterCommand:
    return RegisterCommand(
        email=" Person@Example.COM ",
        password="a long safe password",
        password_confirmation="a long safe password",
    )


def test_commits_user_and_exact_starter_catalogs_once() -> None:
    uow = FakeUnitOfWork()
    service, unsafe, hasher = registration_service(uow)

    result = service.register(valid_command())

    assert result.user_id == 42
    assert result.normalized_email == "person@example.com"
    assert result.preferred_currency == "COP"
    assert unsafe.checked == ["a long safe password"]
    assert hasher.seen == ["a long safe password"]
    assert uow.users.added == [
        {
            "email": "person@example.com",
            "password_hash": "$argon2id$test-hash",
            "preferred_currency_id": 7,
        }
    ]
    assert uow.catalogs.calls == [(42, REGISTRATION_CATALOG_TEMPLATE)]
    assert uow.flushes == 1
    assert uow.commits == 1
    assert uow.rollbacks == 0


def test_known_duplicate_rolls_back_without_staging_user() -> None:
    uow = FakeUnitOfWork(exists=True)
    service, _, _ = registration_service(uow)

    with pytest.raises(EmailAlreadyRegisteredError):
        service.register(valid_command())

    assert uow.users.added == []
    assert uow.catalogs.calls == []
    assert uow.commits == 0
    assert uow.rollbacks == 1


def test_staging_failure_rolls_back_the_complete_unit() -> None:
    uow = FakeUnitOfWork(catalog_failure=True)
    service, _, _ = registration_service(uow)

    with pytest.raises(RuntimeError, match="staging failed"):
        service.register(valid_command())

    assert len(uow.users.added) == 1
    assert uow.commits == 0
    assert uow.rollbacks == 1


def test_email_unique_constraint_race_maps_to_public_conflict() -> None:
    class Diagnostic:
        constraint_name = "uq_app_user_email"

    class DriverError(Exception):
        diag = Diagnostic()

    integrity_error = IntegrityError("insert", {}, DriverError())
    uow = FakeUnitOfWork(add_error=integrity_error)
    service, _, _ = registration_service(uow)

    with pytest.raises(EmailAlreadyRegisteredError):
        service.register(valid_command())

    assert uow.commits == 0
    assert uow.rollbacks == 1

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from finance_ai.accounts.policy import AccountPolicy
from finance_ai.accounts.schemas import AccountCreateCommand, AccountType
from finance_ai.accounts.service import AccountService
from finance_ai.core.config import get_settings
from finance_ai.core.errors import (
    IncomeAccountUnavailableError,
    IncomeNotFoundError,
    IncomeSourceUnavailableError,
)
from finance_ai.db.account_uow import SQLAlchemyAccountUnitOfWork
from finance_ai.db.models.catalogs import IncomeSource
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement, FinancialTransaction
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.db.uow import SQLAlchemyIncomeSourceUnitOfWork
from finance_ai.income_sources.service import IncomeSourceService
from finance_ai.incomes.repository import IncomeRepository
from finance_ai.incomes.schemas import IncomeWriteRequest
from finance_ai.incomes.service import IncomeService

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    os.environ["FINANCE_AI_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_users(migrated_database: None) -> None:
    with get_session_factory()() as session:
        session.execute(text("TRUNCATE TABLE app_user RESTART IDENTITY CASCADE"))
        session.commit()


def create_owner(email: str) -> int:
    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency_id is not None
        user = AppUser(
            email=email,
            password_hash="$argon2id$synthetic",
            preferred_currency_id=currency_id,
            is_active=True,
        )
        session.add(user)
        session.commit()
        return user.id


def create_account(owner_id: int, name: str) -> int:
    service = AccountService(
        policy=AccountPolicy(),
        uow_factory=lambda: SQLAlchemyAccountUnitOfWork(get_session_factory()),
    )
    return service.create(
        owner_id=owner_id,
        command=AccountCreateCommand(
            name=name,
            account_type=AccountType.CASH,
            currency_code="COP",
            opening_balance=Decimal("100.00"),
        ),
    ).id


def income_request(
    account_id: int, *, amount: str = "20.00", source_id: int | None = None
) -> IncomeWriteRequest:
    return IncomeWriteRequest(
        account_id=account_id,
        amount=amount,
        date=date(2026, 9, 18),
        income_source_id=source_id,
    )


def account_balance(owner_id: int, account_id: int) -> Decimal:
    service = AccountService(
        policy=AccountPolicy(),
        uow_factory=lambda: SQLAlchemyAccountUnitOfWork(get_session_factory()),
    )
    return service.get(owner_id=owner_id, account_id=account_id).balance


def test_income_lifecycle_recalculates_once_and_reverses_on_delete() -> None:
    owner = create_owner("income-owner@example.com")
    first = create_account(owner, "Cash")
    second = create_account(owner, "Bank")
    with get_session_factory()() as session:
        source = IncomeSource(user_id=owner, name="Salary", is_active=True)
        session.add(source)
        session.commit()
        source_id = source.id
    service = IncomeService(get_session_factory())

    created = service.create(owner_id=owner, request=income_request(first, source_id=source_id))
    assert created.income_source_name == "Salary"
    assert account_balance(owner, first) == Decimal("120.00")
    assert [item.id for item in service.list(owner_id=owner, account_id=first)] == [created.id]

    IncomeSourceService(
        uow_factory=lambda: SQLAlchemyIncomeSourceUnitOfWork(get_session_factory())
    ).delete(user_id=owner, income_source_id=source_id)
    assert service.get(owner_id=owner, income_id=created.id).income_source_id is None

    updated = service.update(
        owner_id=owner,
        income_id=created.id,
        request=income_request(first, amount="30.00"),
    )
    assert updated.income_source_id is None
    assert account_balance(owner, first) == Decimal("130.00")
    with get_session_factory()() as session:
        assert (
            session.scalar(
                select(func.count(AccountMovement.id)).where(
                    AccountMovement.transaction_id == created.id
                )
            )
            == 1
        )

    service.update(
        owner_id=owner,
        income_id=created.id,
        request=income_request(second, amount="40.00"),
    )
    assert account_balance(owner, first) == Decimal("100.00")
    assert account_balance(owner, second) == Decimal("140.00")
    service.delete(owner_id=owner, income_id=created.id)
    assert service.list(owner_id=owner) == []
    assert account_balance(owner, second) == Decimal("100.00")


def test_foreign_and_inactive_references_are_rejected_without_partial_writes() -> None:
    owner = create_owner("income-a@example.com")
    other = create_owner("income-b@example.com")
    own_account = create_account(owner, "Own cash")
    foreign_account = create_account(other, "Other cash")
    with get_session_factory()() as session:
        foreign_source = IncomeSource(user_id=other, name="Private", is_active=True)
        session.add(foreign_source)
        session.commit()
        source_id = foreign_source.id
    service = IncomeService(get_session_factory())

    with pytest.raises(IncomeAccountUnavailableError):
        service.create(owner_id=owner, request=income_request(foreign_account))
    with pytest.raises(IncomeSourceUnavailableError):
        service.create(owner_id=owner, request=income_request(own_account, source_id=source_id))
    AccountService(
        policy=AccountPolicy(),
        uow_factory=lambda: SQLAlchemyAccountUnitOfWork(get_session_factory()),
    ).set_active(owner_id=owner, account_id=own_account, is_active=False)
    with pytest.raises(IncomeAccountUnavailableError):
        service.create(owner_id=owner, request=income_request(own_account))
    assert service.list(owner_id=owner) == []
    created = service.create(owner_id=other, request=income_request(foreign_account))
    with pytest.raises(IncomeNotFoundError):
        service.get(owner_id=owner, income_id=created.id)
    with pytest.raises(IncomeNotFoundError):
        service.update(owner_id=owner, income_id=created.id, request=income_request(own_account))
    with pytest.raises(IncomeNotFoundError):
        service.delete(owner_id=owner, income_id=created.id)
    assert account_balance(other, foreign_account) == Decimal("120.00")


def test_failed_create_rolls_back_transaction_and_movement(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = create_owner("rollback@example.com")
    account_id = create_account(owner, "Cash")
    original = IncomeRepository.create

    def fail_after_staging(
        repository: IncomeRepository,
        *,
        owner_id: int,
        request: IncomeWriteRequest,
        capture_source: str = "manual",
        source_proposal_id: UUID | None = None,
    ) -> int:
        original(
            repository,
            owner_id=owner_id,
            request=request,
            capture_source=capture_source,
            source_proposal_id=source_proposal_id,
        )
        raise RuntimeError("synthetic failure after flush")

    monkeypatch.setattr(IncomeRepository, "create", fail_after_staging)
    with pytest.raises(RuntimeError):
        IncomeService(get_session_factory()).create(
            owner_id=owner, request=income_request(account_id)
        )
    with get_session_factory()() as session:
        assert (
            session.scalar(
                select(func.count(FinancialTransaction.id)).where(
                    FinancialTransaction.user_id == owner,
                    FinancialTransaction.transaction_type == "income",
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(AccountMovement.id)).where(AccountMovement.user_id == owner)
            )
            == 0
        )
    assert account_balance(owner, account_id) == Decimal("100.00")

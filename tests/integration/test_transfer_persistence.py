from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

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
    TransferAccountUnavailableError,
    TransferDestinationAmountRequiredError,
    TransferInsufficientFundsError,
    TransferSameAccountError,
    TransferSourceDestinationMismatchError,
)
from finance_ai.db.account_uow import SQLAlchemyAccountUnitOfWork
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement, FinancialTransaction
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.transfers.schemas import TransferCreateRequest
from finance_ai.transfers.service import TransferService

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


def create_account(
    owner_id: int, name: str, currency: str = "COP", opening: str = "1000.00"
) -> int:
    service = AccountService(
        policy=AccountPolicy(),
        uow_factory=lambda: SQLAlchemyAccountUnitOfWork(get_session_factory()),
    )
    return service.create(
        owner_id=owner_id,
        command=AccountCreateCommand(
            name=name,
            account_type=AccountType.CASH,
            currency_code=currency,
            opening_balance=Decimal(opening),
        ),
    ).id


def transfer_service() -> TransferService:
    return TransferService(get_session_factory())


def transfer_request(
    source_id: int,
    destination_id: int,
    source_amount: str = "100.00",
    destination_amount: str | None = None,
) -> TransferCreateRequest:
    return TransferCreateRequest(
        source_account_id=source_id,
        destination_account_id=destination_id,
        source_amount=source_amount,
        destination_amount=destination_amount,
        date=date(2026, 9, 18),
    )


def balance(owner_id: int, account_id: int) -> Decimal:
    service = AccountService(
        policy=AccountPolicy(),
        uow_factory=lambda: SQLAlchemyAccountUnitOfWork(get_session_factory()),
    )
    return service.get(owner_id=owner_id, account_id=account_id).balance


def transaction_count() -> int:
    with get_session_factory()() as session:
        return int(session.scalar(select(func.count(FinancialTransaction.id))) or 0)


def signed_total(owner_id: int, transaction_type: str) -> Decimal:
    with get_session_factory()() as session:
        total = session.scalar(
            select(func.coalesce(func.sum(AccountMovement.amount), Decimal("0.00")))
            .join(
                FinancialTransaction,
                (FinancialTransaction.id == AccountMovement.transaction_id)
                & (FinancialTransaction.user_id == AccountMovement.user_id),
            )
            .where(
                AccountMovement.user_id == owner_id,
                FinancialTransaction.transaction_type == transaction_type,
            )
        )
        return Decimal(total)


def test_same_currency_transfer_moves_amount_and_skips_income_expense() -> None:
    owner = create_owner("transfer-owner@example.com")
    source = create_account(owner, "Cash")
    destination = create_account(owner, "Savings")

    record = transfer_service().create(
        owner_id=owner, request=transfer_request(source, destination)
    )

    assert record.source_amount == Decimal("100.00")
    assert record.destination_amount == Decimal("100.00")
    assert record.source_account.currency_code == "COP"
    assert balance(owner, source) == Decimal("900.00")
    assert balance(owner, destination) == Decimal("1100.00")
    assert signed_total(owner, "income") == Decimal("0.00")
    assert signed_total(owner, "expense") == Decimal("0.00")
    assert signed_total(owner, "transfer") == Decimal("0.00")


def test_cross_currency_transfer_stores_both_actual_amounts() -> None:
    owner = create_owner("cross-owner@example.com")
    source = create_account(owner, "Dollars", currency="USD")
    destination = create_account(owner, "Pesos", currency="COP")

    record = transfer_service().create(
        owner_id=owner,
        request=transfer_request(source, destination, "100.00", "410000.00"),
    )

    assert record.source_amount == Decimal("100.00")
    assert record.destination_amount == Decimal("410000.00")
    assert record.source_account.currency_code == "USD"
    assert record.destination_account.currency_code == "COP"
    assert balance(owner, source) == Decimal("900.00")
    assert balance(owner, destination) == Decimal("411000.00")


def test_transfer_beyond_source_balance_is_rejected_and_saves_nothing() -> None:
    owner = create_owner("overdraft-owner@example.com")
    source = create_account(owner, "Cash", opening="1000000.00")
    destination = create_account(owner, "Savings", opening="500000.00")

    with pytest.raises(TransferInsufficientFundsError):
        transfer_service().create(
            owner_id=owner,
            request=transfer_request(source, destination, "9000000.00"),
        )
    assert transaction_count() == 0
    assert balance(owner, source) == Decimal("1000000.00")
    assert balance(owner, destination) == Decimal("500000.00")

    # The whole balance is always transferable, but one cent more is not.
    transfer_service().create(
        owner_id=owner, request=transfer_request(source, destination, "1000000.00")
    )
    with pytest.raises(TransferInsufficientFundsError):
        transfer_service().create(
            owner_id=owner,
            request=transfer_request(destination, source, "500000.01"),
        )
    assert balance(owner, source) == Decimal("0.00")
    assert balance(owner, destination) == Decimal("1500000.00")


def test_transfer_rules_save_nothing_on_failure() -> None:
    owner = create_owner("rules-owner@example.com")
    other = create_owner("rules-other@example.com")
    source = create_account(owner, "Cash")
    destination = create_account(owner, "Savings")
    foreign = create_account(other, "Foreign")
    assert transaction_count() == 0

    with pytest.raises(TransferSameAccountError):
        transfer_service().create(owner_id=owner, request=transfer_request(source, source))
    with pytest.raises(TransferAccountUnavailableError):
        transfer_service().create(owner_id=owner, request=transfer_request(source, foreign))
    with pytest.raises(TransferSourceDestinationMismatchError):
        transfer_service().create(
            owner_id=owner, request=transfer_request(source, destination, "100.00", "90.00")
        )
    with pytest.raises(TransferDestinationAmountRequiredError):
        transfer_service().create(
            owner_id=owner,
            request=transfer_request(create_account(owner, "Dollars", currency="USD"), destination),
        )
    assert transaction_count() == 0
    assert balance(owner, source) == Decimal("1000.00")
    assert balance(owner, destination) == Decimal("1000.00")

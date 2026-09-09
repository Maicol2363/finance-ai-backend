from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from finance_ai.accounts.policy import AccountPolicy
from finance_ai.accounts.schemas import AccountCreateCommand, AccountType, AccountUpdateCommand
from finance_ai.accounts.service import AccountService
from finance_ai.core.config import get_settings
from finance_ai.core.errors import (
    AccountCurrencyLockedError,
    AccountDeleteBlockedError,
    AccountNameConflictError,
    AccountNotFoundError,
    AccountReactivationConflictError,
)
from finance_ai.db.account_uow import SQLAlchemyAccountUnitOfWork
from finance_ai.db.models.accounts import Account
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement, FinancialTransaction
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")
NOW = datetime(2026, 9, 7, 19, 30, tzinfo=UTC)


def require_safe_test_database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    database_name = make_url(TEST_DATABASE_URL).database or ""
    if not database_name.endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    return TEST_DATABASE_URL


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    database_url = require_safe_test_database_url()
    os.environ["FINANCE_AI_DATABASE_URL"] = database_url
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_account_data(migrated_database: None) -> None:
    with get_session_factory()() as session:
        session.execute(text("TRUNCATE TABLE app_user RESTART IDENTITY CASCADE"))
        session.commit()


def account_service() -> AccountService:
    return AccountService(
        policy=AccountPolicy(),
        uow_factory=lambda: SQLAlchemyAccountUnitOfWork(get_session_factory()),
    )


def create_user(email: str = "person@example.com") -> int:
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
    *, owner_id: int, name: str = "Cash", currency: str = "COP", opening: str = "100.00"
) -> int:
    result = account_service().create(
        owner_id=owner_id,
        command=AccountCreateCommand(
            name=name,
            account_type=AccountType.CASH,
            currency_code=currency,
            opening_balance=Decimal(opening),
        ),
    )
    return result.id


def add_movement(*, owner_id: int, account_id: int, status: str, amount: str) -> None:
    with get_session_factory()() as session:
        transaction = FinancialTransaction(
            user_id=owner_id,
            transaction_type="adjustment",
            status=status,
            occurred_at=NOW,
            capture_source="manual",
        )
        session.add(transaction)
        session.flush()
        session.add(
            AccountMovement(
                user_id=owner_id,
                transaction_id=transaction.id,
                account_id=account_id,
                amount=Decimal(amount),
            )
        )
        session.commit()


def test_migration_constraints_and_model_metadata_match() -> None:
    with get_session_factory()() as session:
        check_names = set(
            session.scalars(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid IN (
                        'account'::regclass,
                        'financial_transaction'::regclass,
                        'account_movement'::regclass
                    )
                    AND contype = 'c'
                    """
                )
            )
        )

    assert check_names == {
        "ck_account_movement_amount_non_zero",
        "ck_account_name_trimmed_non_empty",
        "ck_account_type_supported",
        "ck_financial_transaction_capture_source_supported",
        "ck_financial_transaction_status_supported",
        "ck_financial_transaction_type_supported",
    }


def test_balance_uses_only_confirmed_movements_and_currency_then_locks() -> None:
    owner_id = create_user()
    account_id = create_account(owner_id=owner_id)
    add_movement(owner_id=owner_id, account_id=account_id, status="confirmed", amount="-20.25")
    add_movement(owner_id=owner_id, account_id=account_id, status="confirmed", amount="20.25")
    add_movement(owner_id=owner_id, account_id=account_id, status="pending", amount="-90.00")
    add_movement(owner_id=owner_id, account_id=account_id, status="voided", amount="400.00")

    result = account_service().get(owner_id=owner_id, account_id=account_id)

    assert result.opening_balance == Decimal("100.00")
    assert result.balance == Decimal("100.00")
    assert result.currency_is_locked is True
    with pytest.raises(AccountCurrencyLockedError):
        account_service().update(
            owner_id=owner_id,
            account_id=account_id,
            command=AccountUpdateCommand(currency_code="USD"),
        )


def test_inactive_name_reuse_and_reactivation_conflict_are_database_authoritative() -> None:
    owner_id = create_user()
    old_id = create_account(owner_id=owner_id, name="Savings")
    service = account_service()
    service.set_active(owner_id=owner_id, account_id=old_id, is_active=False)
    replacement_id = create_account(owner_id=owner_id, name="sAvInGs")

    with pytest.raises(AccountReactivationConflictError):
        service.set_active(owner_id=owner_id, account_id=old_id, is_active=True)

    result = service.list(owner_id=owner_id, preferred_currency_code="COP", include_inactive=True)
    assert [item.id for item in result.accounts] == [replacement_id, old_id]


def test_owner_queries_and_composite_movement_foreign_keys_fail_closed() -> None:
    first_owner = create_user("first@example.com")
    second_owner = create_user("second@example.com")
    account_id = create_account(owner_id=first_owner)

    with pytest.raises(AccountNotFoundError):
        account_service().get(owner_id=second_owner, account_id=account_id)

    with get_session_factory()() as session:
        transaction = FinancialTransaction(
            user_id=second_owner,
            transaction_type="adjustment",
            status="confirmed",
            occurred_at=NOW,
            capture_source="manual",
        )
        session.add(transaction)
        session.flush()
        session.add(
            AccountMovement(
                user_id=second_owner,
                transaction_id=transaction.id,
                account_id=account_id,
                amount=Decimal("1.00"),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    assert account_service().get(owner_id=first_owner, account_id=account_id).balance == Decimal(
        "100.00"
    )


def test_delete_unused_account_and_block_every_movement_status() -> None:
    owner_id = create_user()
    service = account_service()
    unused_id = create_account(owner_id=owner_id, name="Unused", opening="975.25")

    service.delete(owner_id=owner_id, account_id=unused_id)

    with pytest.raises(AccountNotFoundError):
        service.get(owner_id=owner_id, account_id=unused_id)
    for index, movement_status in enumerate(("pending", "confirmed", "voided"), start=1):
        account_id = create_account(owner_id=owner_id, name=f"Used {index}")
        add_movement(
            owner_id=owner_id,
            account_id=account_id,
            status=movement_status,
            amount="-1.00",
        )

        with pytest.raises(AccountDeleteBlockedError):
            service.delete(owner_id=owner_id, account_id=account_id)

        assert service.get(owner_id=owner_id, account_id=account_id).id == account_id


def test_concurrent_movement_and_delete_never_orphan_history() -> None:
    owner_id = create_user()
    account_id = create_account(owner_id=owner_id, name="Race account")
    start = Barrier(2)

    def attempt_delete() -> str:
        start.wait()
        try:
            account_service().delete(owner_id=owner_id, account_id=account_id)
        except AccountDeleteBlockedError:
            return "delete_blocked"
        return "deleted"

    def attempt_movement() -> str:
        start.wait()
        try:
            add_movement(
                owner_id=owner_id,
                account_id=account_id,
                status="pending",
                amount="-1.00",
            )
        except IntegrityError:
            return "movement_blocked"
        return "movement_created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(attempt_delete)
        movement_future = executor.submit(attempt_movement)
        outcomes = {delete_future.result(), movement_future.result()}

    assert outcomes in (
        {"deleted", "movement_blocked"},
        {"delete_blocked", "movement_created"},
    )
    with get_session_factory()() as session:
        account_exists = session.scalar(select(Account.id).where(Account.id == account_id))
        movement_count = session.scalar(
            select(AccountMovement.id).where(AccountMovement.account_id == account_id)
        )
    if "deleted" in outcomes:
        assert account_exists is None
        assert movement_count is None
    else:
        assert account_exists == account_id
        assert movement_count is not None


def test_concurrent_normalized_names_create_exactly_one_active_account() -> None:
    owner_id = create_user()

    def attempt(name: str) -> str:
        try:
            create_account(owner_id=owner_id, name=name)
        except AccountNameConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ["Wallet", "wallet"]))

    assert sorted(results) == ["conflict", "created"]
    with get_session_factory()() as session:
        accounts = session.scalars(select(Account).where(Account.user_id == owner_id)).all()
    assert len(accounts) == 1


def test_seeded_owner_and_balance_queries_have_index_backed_plans() -> None:
    owner_id = create_user()
    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency_id is not None
        session.add_all(
            [
                Account(
                    user_id=owner_id,
                    name=f"Account {index:03d}",
                    account_type="bank",
                    currency_id=currency_id,
                    opening_balance=Decimal("0.00"),
                    is_active=True,
                )
                for index in range(100)
            ]
        )
        session.commit()
        account_id = session.scalar(
            select(Account.id).where(Account.user_id == owner_id).order_by(Account.id).limit(1)
        )
        assert account_id is not None
        session.execute(text("SET LOCAL enable_seqscan = off"))
        account_plan = "\n".join(
            session.scalars(
                text(
                    """
                    EXPLAIN (COSTS OFF)
                    SELECT id
                    FROM account
                    WHERE user_id = :owner_id AND is_active = true
                    ORDER BY lower(name), id
                    """
                ),
                {"owner_id": owner_id},
            )
        )
        movement_plan = "\n".join(
            session.scalars(
                text(
                    """
                    EXPLAIN (COSTS OFF)
                    SELECT sum(amount)
                    FROM account_movement
                    WHERE user_id = :owner_id AND account_id = :account_id
                    """
                ),
                {"owner_id": owner_id, "account_id": account_id},
            )
        )

    assert "uq_account_active_name" in account_plan or "ix_account_user_active_name" in account_plan
    assert "ix_account_movement_user_account" in movement_plan

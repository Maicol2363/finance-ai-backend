from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.accounts.repositories import (
    AccountMovementConstraintError,
    ActiveAccountNameConstraintError,
)
from finance_ai.accounts.schemas import AccountType
from finance_ai.db.account_uow import SQLAlchemyAccountUnitOfWork
from finance_ai.db.base import Base
from finance_ai.db.models.accounts import Account
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement, FinancialTransaction
from finance_ai.db.repositories.accounts import (
    SQLAlchemyAccountCurrencyRepository,
    SQLAlchemyAccountRepository,
    is_account_movement_constraint,
    is_active_name_conflict,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.fixture
def account_session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def install_btrim(dbapi_connection: object, connection_record: object) -> None:
        del connection_record
        dbapi_connection.create_function("btrim", 1, lambda value: value.strip())  # type: ignore[attr-defined]
        dbapi_connection.create_function("char_length", 1, len)  # type: ignore[attr-defined]

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        cop = Currency(id=1, code="COP", name="Colombian Peso", decimals=2, is_active=True)
        usd = Currency(id=2, code="USD", name="US Dollar", decimals=2, is_active=True)
        session.add_all((cop, usd))
        session.flush()
        first_user = AppUser(
            id=1,
            email="first@example.com",
            password_hash="synthetic",
            preferred_currency_id=1,
            is_active=True,
        )
        second_user = AppUser(
            id=2,
            email="second@example.com",
            password_hash="synthetic",
            preferred_currency_id=1,
            is_active=True,
        )
        session.add_all((first_user, second_user))
        session.flush()
        first = Account(
            id=10,
            user_id=1,
            name="Cash",
            account_type="cash",
            currency_id=1,
            opening_balance=Decimal("100.00"),
            is_active=True,
        )
        inactive = Account(
            id=11,
            user_id=1,
            name="Old wallet",
            account_type="digital_wallet",
            currency_id=2,
            opening_balance=Decimal("10.00"),
            is_active=False,
        )
        foreign = Account(
            id=12,
            user_id=2,
            name="Foreign",
            account_type="bank",
            currency_id=1,
            opening_balance=Decimal("999.00"),
            is_active=True,
        )
        session.add_all((first, inactive, foreign))
        session.flush()
        confirmed = FinancialTransaction(
            id=20,
            user_id=1,
            transaction_type="adjustment",
            status="confirmed",
            occurred_at=NOW,
            capture_source="manual",
        )
        pending = FinancialTransaction(
            id=21,
            user_id=1,
            transaction_type="adjustment",
            status="pending",
            occurred_at=NOW,
            capture_source="manual",
        )
        session.add_all((confirmed, pending))
        session.flush()
        session.add_all(
            (
                AccountMovement(
                    id=30,
                    user_id=1,
                    transaction_id=20,
                    account_id=10,
                    amount=Decimal("-25.50"),
                ),
                AccountMovement(
                    id=31,
                    user_id=1,
                    transaction_id=21,
                    account_id=10,
                    amount=Decimal("-90.00"),
                ),
            )
        )
        session.commit()
    yield factory
    engine.dispose()


def test_repository_projects_owner_scoped_confirmed_balances_and_mutations(
    account_session_factory: sessionmaker[Session],
) -> None:
    with account_session_factory() as session:
        repository = SQLAlchemyAccountRepository(session)

        assert [item.id for item in repository.list_owned(owner_id=1, include_inactive=False)] == [
            10
        ]
        assert [item.id for item in repository.list_owned(owner_id=1, include_inactive=True)] == [
            10,
            11,
        ]
        account = repository.find_owned(owner_id=1, account_id=10, for_update=True)
        assert account is not None and account.balance == Decimal("74.50")
        assert repository.find_owned(owner_id=2, account_id=10) is None
        assert repository.find_active_owned(owner_id=1, account_id=11) is None
        assert repository.active_name_exists(owner_id=1, normalized_name="cash") is True
        assert (
            repository.active_name_exists(
                owner_id=1,
                normalized_name="cash",
                excluding_id=10,
            )
            is False
        )
        assert repository.has_confirmed_movements(owner_id=1, account_id=10) is True
        assert repository.has_movements(owner_id=1, account_id=10) is True
        assert repository.has_movements(owner_id=1, account_id=11) is False
        assert repository.latest_active_currency_code(owner_id=1) == "USD"

        repository.update_metadata(
            owner_id=1,
            account_id=10,
            name="Pocket",
            account_type=AccountType.OTHER,
            currency_id=2,
            opening_balance=Decimal("125.00"),
        )
        repository.set_active(owner_id=1, account_id=10, is_active=False)
        session.flush()
        updated = repository.find_owned(owner_id=1, account_id=10)
        assert updated is not None
        assert (updated.name, updated.account_type, updated.currency.code) == (
            "Pocket",
            AccountType.OTHER,
            "USD",
        )
        assert updated.balance == Decimal("99.50")
        assert updated.is_active is False

        repository.delete(owner_id=1, account_id=11)
        session.flush()
        assert repository.find_owned(owner_id=1, account_id=11) is None


def test_currency_repository_resolves_only_available_options(
    account_session_factory: sessionmaker[Session],
) -> None:
    with account_session_factory() as session:
        currencies = SQLAlchemyAccountCurrencyRepository(session)

        assert [item.code for item in currencies.list_active()] == ["COP", "USD"]
        assert currencies.find_active_by_code("COP") is not None
        assert currencies.find_active_by_code("EUR") is None
        assert currencies.first_active_code(("EUR", "USD", "COP")) == "USD"
        assert currencies.first_active_code(()) is None


def test_account_unit_of_work_commits_or_rolls_back_and_closes(
    account_session_factory: sessionmaker[Session],
) -> None:
    with SQLAlchemyAccountUnitOfWork(account_session_factory) as committed:
        committed.flush()
        committed.commit()
        committed_session = committed.session
    assert committed_session is not None

    with SQLAlchemyAccountUnitOfWork(account_session_factory) as rolled_back:
        rolled_back.rollback()
        rolled_back_session = rolled_back.session
    assert rolled_back_session is not None

    unopened = SQLAlchemyAccountUnitOfWork(account_session_factory)
    with pytest.raises(RuntimeError, match="has not been entered"):
        unopened.flush()


def test_only_the_named_constraint_is_recognized() -> None:
    class Diagnostic:
        constraint_name = "uq_account_active_name"

    class DriverFailure(Exception):
        diag = Diagnostic()

    known = IntegrityError("synthetic", {}, DriverFailure())
    unknown = IntegrityError("synthetic", {}, RuntimeError("unknown"))

    assert is_active_name_conflict(known) is True
    assert is_active_name_conflict(unknown) is False
    assert is_active_name_conflict(RuntimeError("not integrity")) is False
    assert issubclass(ActiveAccountNameConstraintError, Exception)

    Diagnostic.constraint_name = "fk_account_movement_account_owner"
    movement_known = IntegrityError("synthetic", {}, DriverFailure())
    assert is_account_movement_constraint(movement_known) is True
    assert is_account_movement_constraint(unknown) is False
    assert is_account_movement_constraint(RuntimeError("not integrity")) is False
    assert issubclass(AccountMovementConstraintError, Exception)

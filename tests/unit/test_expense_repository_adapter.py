"""Unit tests for SQLAlchemyExpenseRepository adapter methods."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.db.base import Base
from finance_ai.db.models.accounts import Account
from finance_ai.db.models.catalogs import Category
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.repositories.expenses import SQLAlchemyExpenseRepository
from finance_ai.receipts.schemas import ConfirmedReceiptItem

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def expense_session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def install_sqlite_functions(dbapi_connection: object, connection_record: object) -> None:
        del connection_record
        dbapi_connection.create_function("btrim", 1, lambda value: value.strip())  # type: ignore[attr-defined]
        dbapi_connection.create_function("char_length", 1, len)  # type: ignore[attr-defined]

    _counters: dict[str, int] = {}

    @event.listens_for(Session, "before_flush")
    def assign_ids_for_sqlite(session: Session, flush_context: object, instances: object) -> None:
        for obj in session.new:
            table_name = getattr(obj, "__tablename__", None)
            if table_name and getattr(obj, "id", None) is None:
                _counters[table_name] = _counters.get(table_name, 100) + 1
                obj.id = _counters[table_name]

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        cop = Currency(id=1, code="COP", name="Colombian Peso", decimals=2, is_active=True)
        session.add(cop)
        user = AppUser(
            id=1,
            email="test@gmail.com",
            password_hash="mockhash",
            preferred_currency_id=1,
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        account = Account(
            id=1,
            user_id=1,
            currency_id=1,
            name="Bancolombia",
            account_type="bank",
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(account)
        category = Category(
            id=1,
            user_id=1,
            name="Supermercado",
            color="green",
            icon="shopping",
            sort_order=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(category)
        session.commit()
    return factory


def test_create_from_receipt_proposal_and_lookup(
    expense_session_factory: sessionmaker[Session],
) -> None:
    with expense_session_factory() as session:
        repo = SQLAlchemyExpenseRepository(session)
        items = [
            ConfirmedReceiptItem(
                name="Leche Entera",
                quantity=Decimal("2"),
                unit_price=Decimal("4500.00"),
                total_price=Decimal("9000.00"),
                category_id=1,
                note=None,
            ),
            ConfirmedReceiptItem(
                name="Pan Tajado",
                quantity=Decimal("1"),
                unit_price=Decimal("2700.00"),
                total_price=Decimal("2700.00"),
                category_id=1,
                note=None,
            ),
        ]
        created = repo.create_from_receipt_proposal(
            owner_id=1,
            account_id=1,
            total_amount=Decimal("11700.00"),
            merchant="D1",
            occurred_at=NOW,
            category_id=1,
            items=items,
            note="Compra semanal",
        )
        session.commit()

        assert created.store == "D1"
        assert created.amount == Decimal("9000.00")
        assert created.receipt_total == Decimal("11700.00")
        assert len(created.items) == 2
        assert created.items[0].name == "Leche Entera"
        assert created.items[1].name == "Pan Tajado"

        by_tx = repo.find_by_transaction_id(owner_id=1, transaction_id=created.transaction_id)
        assert by_tx is not None
        assert by_tx.id == created.id
        assert len(by_tx.items) == 2

        duplicate_exists = repo.find_potential_duplicate(
            owner_id=1,
            merchant="d1 ",
            total_amount=Decimal("11700.00"),
            occurred_date=NOW.date(),
        )
        assert duplicate_exists is True

        no_duplicate = repo.find_potential_duplicate(
            owner_id=1,
            merchant="Éxito",
            total_amount=Decimal("11700.00"),
            occurred_date=NOW.date(),
        )
        assert no_duplicate is False

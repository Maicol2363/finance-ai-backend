"""Unit tests for SQLAlchemyExpenseRepository adapter methods."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.db.base import Base
from finance_ai.db.models.accounts import Account
from finance_ai.db.models.catalogs import Category
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.receipts import Expense
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement
from finance_ai.db.repositories.expenses import SQLAlchemyExpenseRepository
from finance_ai.expenses.repositories import ExpenseItemInput
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


def _create_receipt_with_items(
    repo: SQLAlchemyExpenseRepository,
    session: Session,
    *,
    count: int = 2,
) -> tuple[int, int]:
    """Creates a receipt expense whose lines are `count` rows of 1000.00 each."""
    items = [
        ConfirmedReceiptItem(
            name=f"Item {index}",
            quantity=Decimal("1"),
            unit_price=Decimal("1000.00"),
            total_price=Decimal("1000.00"),
            category_id=1,
        )
        for index in range(1, count + 1)
    ]
    created = repo.create_from_receipt_proposal(
        owner_id=1,
        account_id=1,
        total_amount=Decimal(count * 1000).quantize(Decimal("0.01")),
        merchant="D1",
        occurred_at=NOW,
        category_id=1,
        items=items,
        note=None,
    )
    session.commit()
    return created.id, created.receipt_id


def test_replace_items_rewrites_rows_and_syncs_total(
    expense_session_factory: sessionmaker[Session],
) -> None:
    with expense_session_factory() as session:
        repo = SQLAlchemyExpenseRepository(session)
        expense_id, receipt_id = _create_receipt_with_items(repo, session, count=3)

        repo.replace_items(
            owner_id=1,
            expense_id=expense_id,
            items=[
                ExpenseItemInput(
                    name="Cafe",
                    total_price=Decimal("2500.00"),
                    unit_price=Decimal("2500.00"),
                    category_id=1,
                ),
                ExpenseItemInput(name="Pan", total_price=Decimal("1500.00")),
            ],
        )
        session.commit()

        updated = repo.find_owned(owner_id=1, expense_id=expense_id)
        assert updated is not None
        assert updated.id == expense_id
        assert [item.name for item in updated.items] == ["Cafe", "Pan"]
        assert [item.amount for item in updated.items] == [
            Decimal("2500.00"),
            Decimal("1500.00"),
        ]
        # An item without a category falls back to the receipt's category.
        assert updated.items[1].category is not None
        assert updated.items[1].category.id == 1
        assert updated.receipt_total == Decimal("4000.00")

        positions = session.scalars(
            select(Expense.position)
            .where(Expense.receipt_id == receipt_id)
            .order_by(Expense.position)
        ).all()
        assert list(positions) == [1, 2]

        movement = session.scalar(
            select(AccountMovement.amount).where(
                AccountMovement.transaction_id == updated.transaction_id
            )
        )
        assert movement == Decimal("-4000.00")


def test_replace_items_grows_the_receipt_and_renumbers_positions(
    expense_session_factory: sessionmaker[Session],
) -> None:
    with expense_session_factory() as session:
        repo = SQLAlchemyExpenseRepository(session)
        expense_id, receipt_id = _create_receipt_with_items(repo, session, count=1)

        repo.replace_items(
            owner_id=1,
            expense_id=expense_id,
            items=[
                ExpenseItemInput(name="Cafe", total_price=Decimal("1000.00")),
                ExpenseItemInput(name="Pan", total_price=Decimal("2000.00")),
                ExpenseItemInput(name="Leche", total_price=Decimal("3000.00")),
            ],
        )
        session.commit()

        updated = repo.find_owned(owner_id=1, expense_id=expense_id)
        assert updated is not None
        assert [item.name for item in updated.items] == ["Cafe", "Pan", "Leche"]
        assert updated.receipt_total == Decimal("6000.00")

        positions = session.scalars(
            select(Expense.position)
            .where(Expense.receipt_id == receipt_id)
            .order_by(Expense.position)
        ).all()
        assert list(positions) == [1, 2, 3]


def test_replace_items_collapses_to_a_single_fallback_row(
    expense_session_factory: sessionmaker[Session],
) -> None:
    with expense_session_factory() as session:
        repo = SQLAlchemyExpenseRepository(session)
        expense_id, receipt_id = _create_receipt_with_items(repo, session, count=2)

        repo.replace_items(
            owner_id=1,
            expense_id=expense_id,
            items=[],
            fallback_amount=Decimal("5000.00"),
        )
        session.commit()

        updated = repo.find_owned(owner_id=1, expense_id=expense_id)
        assert updated is not None
        assert [item.name for item in updated.items] == ["D1"]
        assert updated.items[0].amount == Decimal("5000.00")
        assert updated.receipt_total == Decimal("5000.00")

        positions = session.scalars(
            select(Expense.position).where(Expense.receipt_id == receipt_id)
        ).all()
        assert list(positions) == [1]

        movement = session.scalar(
            select(AccountMovement.amount).where(
                AccountMovement.transaction_id == updated.transaction_id
            )
        )
        assert movement == Decimal("-5000.00")


def test_replace_items_ignores_expenses_the_owner_does_not_own(
    expense_session_factory: sessionmaker[Session],
) -> None:
    with expense_session_factory() as session:
        repo = SQLAlchemyExpenseRepository(session)
        expense_id, _ = _create_receipt_with_items(repo, session, count=2)

        repo.replace_items(
            owner_id=2,
            expense_id=expense_id,
            items=[ExpenseItemInput(name="Hijacked", total_price=Decimal("1.00"))],
        )
        session.commit()

        updated = repo.find_owned(owner_id=1, expense_id=expense_id)
        assert updated is not None
        assert [item.name for item in updated.items] == ["Item 1", "Item 2"]

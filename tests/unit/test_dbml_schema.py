import datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.db.base import Base
from finance_ai.db.models.budgets import SpendingLimit
from finance_ai.db.models.catalogs import (
    Category,
    Incomesource,
    PaymentMethod,
    Subcategory,
)
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.income import Income
from finance_ai.db.models.receipts import Expense, Receipt
from finance_ai.db.models.reference import Currency


@event.listens_for(Engine, "connect")
def add_sqlite_functions(dbapi_connection, connection_record) -> None:
    if hasattr(dbapi_connection, "create_function"):
        dbapi_connection.create_function("btrim", 1, lambda s: s.strip() if s is not None else None)
        dbapi_connection.create_function(
            "char_length", 1, lambda s: len(s) if s is not None else None
        )


@pytest.fixture
def sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine)()
    try:
        yield session_local
    finally:
        session_local.close()


def test_dbml_roles_and_user_creation(sqlite_session: Session) -> None:
    role_user = Role(name="user")
    role_admin = Role(name="admin")
    currency = Currency(code="COP", name="Colombian Peso", decimals=2)
    sqlite_session.add_all([role_user, role_admin, currency])
    sqlite_session.commit()

    user = AppUser(
        email="test@example.com",
        password_hash="hash123",
        role_id=role_user.id,
        preferred_currency_id=currency.id,
    )
    sqlite_session.add(user)
    sqlite_session.commit()

    assert user.id is not None
    assert user.role.name == "user"
    assert user.preferred_currency.code == "COP"


def test_category_and_subcategory_relationship(sqlite_session: Session) -> None:
    role = Role(name="user")
    currency = Currency(code="USD", name="US Dollar", decimals=2)
    sqlite_session.add_all([role, currency])
    sqlite_session.commit()

    user = AppUser(
        email="subcat@example.com",
        password_hash="hash",
        role_id=role.id,
        preferred_currency_id=currency.id,
    )
    sqlite_session.add(user)
    sqlite_session.commit()

    category = Category(user_id=user.id, name="Food", color="blue", icon="food", sort_order=1)
    sqlite_session.add(category)
    sqlite_session.commit()

    sub1 = Subcategory(category_id=category.id, name="Groceries")
    sub2 = Subcategory(category_id=category.id, name="Restaurants")
    sqlite_session.add_all([sub1, sub2])
    sqlite_session.commit()

    assert len(category.subcategories) == 2
    assert {s.name for s in category.subcategories} == {"Groceries", "Restaurants"}


def test_receipt_and_expense_creation(sqlite_session: Session) -> None:
    role = Role(name="user")
    currency = Currency(code="COP", name="Colombian Peso", decimals=2)
    sqlite_session.add_all([role, currency])
    sqlite_session.commit()

    user = AppUser(
        email="receipt@example.com",
        password_hash="hash",
        role_id=role.id,
        preferred_currency_id=currency.id,
    )
    payment_method = PaymentMethod(name="Credit Card")
    sqlite_session.add_all([user, payment_method])
    sqlite_session.commit()

    category = Category(user_id=user.id, name="Shopping", color="red", icon="shopping")
    sqlite_session.add(category)
    sqlite_session.commit()

    subcategory = Subcategory(category_id=category.id, name="Electronics")
    sqlite_session.add(subcategory)
    sqlite_session.commit()

    receipt = Receipt(
        user_id=user.id,
        store="Tech Store",
        date=datetime.date(2026, 9, 13),
        currency_id=currency.id,
        purchase_reason="Work gear",
        total=Decimal("150.00"),
        payment_method_id=payment_method.id,
    )
    sqlite_session.add(receipt)
    sqlite_session.commit()

    expense = Expense(
        user_id=user.id,
        receipt_id=receipt.id,
        subcategory_id=subcategory.id,
        name="Wireless Mouse",
        amount=Decimal("150.00"),
        item_reason="New mouse for work",
    )
    sqlite_session.add(expense)
    sqlite_session.commit()

    assert receipt.id is not None
    assert len(receipt.expenses) == 1
    assert receipt.expenses[0].name == "Wireless Mouse"
    assert receipt.expenses[0].subcategory.name == "Electronics"


def test_spending_limit_xor_target_and_period(sqlite_session: Session) -> None:
    role = Role(name="user")
    currency = Currency(code="USD", name="US Dollar", decimals=2)
    sqlite_session.add_all([role, currency])
    sqlite_session.commit()

    user = AppUser(
        email="limit@example.com",
        password_hash="hash",
        role_id=role.id,
        preferred_currency_id=currency.id,
    )
    sqlite_session.add(user)
    sqlite_session.commit()

    category = Category(user_id=user.id, name="Entertainment", color="purple", icon="entertainment")
    sqlite_session.add(category)
    sqlite_session.commit()

    limit1 = SpendingLimit(
        user_id=user.id,
        name="Monthly Movies",
        amount=Decimal("100.00"),
        period="monthly",
        category_id=category.id,
        subcategory_id=None,
    )
    sqlite_session.add(limit1)
    sqlite_session.commit()

    assert limit1.id is not None
    assert limit1.period == "monthly"
    assert limit1.category.name == "Entertainment"


def test_income_and_incomesource_creation(sqlite_session: Session) -> None:
    role = Role(name="user")
    currency = Currency(code="COP", name="Colombian Peso", decimals=2)
    incomesource = Incomesource(name="Salary")
    sqlite_session.add_all([role, currency, incomesource])
    sqlite_session.commit()

    user = AppUser(
        email="income@example.com",
        password_hash="hash",
        role_id=role.id,
        preferred_currency_id=currency.id,
    )
    sqlite_session.add(user)
    sqlite_session.commit()

    income = Income(
        user_id=user.id,
        income_source_id=incomesource.id,
        currency_id=currency.id,
        amount=Decimal("3000.00"),
        date=datetime.date(2026, 9, 1),
        description="Monthly paycheck",
    )
    sqlite_session.add(income)
    sqlite_session.commit()

    assert income.id is not None
    assert income.income_source.name == "Salary"
    assert income.amount == Decimal("3000.00")

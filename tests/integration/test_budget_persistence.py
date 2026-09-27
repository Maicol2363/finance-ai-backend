from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from finance_ai.budgets.periods import BudgetPeriod
from finance_ai.budgets.policy import BudgetPolicy
from finance_ai.budgets.schemas import BudgetCreateCommand, BudgetUpdateCommand
from finance_ai.budgets.service import BudgetService
from finance_ai.categories.policy import CategoryPolicy
from finance_ai.categories.service import CategoryService
from finance_ai.core.config import get_settings
from finance_ai.core.errors import BudgetNotFoundError
from finance_ai.db.budget_uow import SQLAlchemyBudgetUnitOfWork
from finance_ai.db.category_uow import SQLAlchemyCategoryUnitOfWork
from finance_ai.db.models.budgets import Budget
from finance_ai.db.models.catalogs import Category, PaymentMethod
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.receipts import Expense, Receipt
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import FinancialTransaction
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")
TODAY = date(2026, 9, 27)


def require_safe_test_database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    database_name = make_url(TEST_DATABASE_URL).database or ""
    if not database_name.endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    return TEST_DATABASE_URL


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    os.environ["FINANCE_AI_DATABASE_URL"] = require_safe_test_database_url()
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_data(migrated_database: None) -> None:
    with get_session_factory()() as session:
        session.execute(text("TRUNCATE TABLE app_user RESTART IDENTITY CASCADE"))
        session.commit()


def budget_service() -> BudgetService:
    return BudgetService(
        policy=BudgetPolicy(),
        uow_factory=lambda: SQLAlchemyBudgetUnitOfWork(get_session_factory()),
        today_factory=lambda: TODAY,
    )


def category_service() -> CategoryService:
    return CategoryService(
        policy=CategoryPolicy(),
        uow_factory=lambda: SQLAlchemyCategoryUnitOfWork(get_session_factory()),
    )


def create_user(email: str, preferred: str = "COP") -> int:
    with get_session_factory()() as session:
        role_id = session.scalar(select(Role.id).where(Role.name == "user"))
        currency_id = session.scalar(select(Currency.id).where(Currency.code == preferred))
        assert role_id is not None and currency_id is not None
        user = AppUser(
            email=email,
            password_hash="x" * 20,
            role_id=role_id,
            preferred_currency_id=currency_id,
            is_active=True,
        )
        session.add(user)
        session.commit()
        return user.id


def create_category(user_id: int, name: str) -> int:
    with get_session_factory()() as session:
        category = Category(
            user_id=user_id,
            name=name,
            color="orange",
            icon="food",
            sort_order=0,
        )
        session.add(category)
        session.commit()
        return category.id


def create_expense(
    *,
    user_id: int,
    category_id: int,
    currency_code: str,
    amount: str,
    spent_on: date,
    status: str = "confirmed",
    category_on_receipt_only: bool = False,
) -> None:
    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == currency_code))
        payment_method_id = session.scalar(select(PaymentMethod.id).limit(1))
        assert currency_id is not None and payment_method_id is not None
        value = Decimal(amount)
        transaction = FinancialTransaction(
            user_id=user_id,
            transaction_type="expense",
            status=status,
            occurred_at=datetime.combine(spent_on, datetime.min.time(), tzinfo=UTC),
            description="Budget integration expense",
            capture_source="manual",
        )
        session.add(transaction)
        session.flush()
        receipt = Receipt(
            user_id=user_id,
            transaction_id=transaction.id,
            store="Budget test store",
            date=spent_on,
            currency_id=currency_id,
            category_id=category_id,
            total=value,
            payment_method_id=payment_method_id,
        )
        session.add(receipt)
        session.flush()
        session.add(
            Expense(
                user_id=user_id,
                receipt_id=receipt.id,
                category_id=None if category_on_receipt_only else category_id,
                position=1,
                name="Budget item",
                quantity=Decimal("1"),
                unit_price=value,
                amount=value,
            )
        )
        session.commit()


def create_budget(*, owner_id: int, category_id: int, currency: str, period: BudgetPeriod) -> int:
    return (
        budget_service()
        .create(
            owner_id=owner_id,
            command=BudgetCreateCommand(
                category_id=category_id,
                amount=Decimal("100.00"),
                currency_code=currency,
                period=period,
            ),
        )
        .id
    )


def test_multiple_budget_progress_is_derived_by_period_category_currency_and_status() -> None:
    owner_id = create_user("budget-owner@example.com")
    other_id = create_user("budget-other@example.com")
    food_id = create_category(owner_id, "Food")
    transport_id = create_category(owner_id, "Transport")
    other_food_id = create_category(other_id, "Food")
    weekly_id = create_budget(
        owner_id=owner_id,
        category_id=food_id,
        currency="COP",
        period=BudgetPeriod.WEEKLY,
    )
    monthly_id = create_budget(
        owner_id=owner_id,
        category_id=transport_id,
        currency="USD",
        period=BudgetPeriod.MONTHLY,
    )

    create_expense(
        user_id=owner_id,
        category_id=food_id,
        currency_code="COP",
        amount="10.00",
        spent_on=date(2026, 9, 21),
        category_on_receipt_only=True,
    )
    create_expense(
        user_id=owner_id,
        category_id=food_id,
        currency_code="COP",
        amount="15.00",
        spent_on=date(2026, 9, 27),
    )
    create_expense(
        user_id=owner_id,
        category_id=food_id,
        currency_code="COP",
        amount="90.00",
        spent_on=date(2026, 9, 20),
    )
    create_expense(
        user_id=owner_id,
        category_id=food_id,
        currency_code="USD",
        amount="80.00",
        spent_on=date(2026, 9, 24),
    )
    create_expense(
        user_id=owner_id,
        category_id=food_id,
        currency_code="COP",
        amount="70.00",
        spent_on=date(2026, 9, 25),
        status="pending",
    )
    create_expense(
        user_id=owner_id,
        category_id=transport_id,
        currency_code="USD",
        amount="30.00",
        spent_on=date(2026, 9, 1),
    )
    create_expense(
        user_id=other_id,
        category_id=other_food_id,
        currency_code="COP",
        amount="99.00",
        spent_on=date(2026, 9, 23),
    )

    result = budget_service().list(owner_id=owner_id, preferred_currency_code="COP")
    by_id = {item.id: item for item in result.budgets}

    assert len(result.budgets) == 2
    assert by_id[weekly_id].spent_amount == Decimal("25.00")
    assert by_id[monthly_id].spent_amount == Decimal("30.00")
    assert by_id[weekly_id].current_period_start == date(2026, 9, 21)
    assert by_id[weekly_id].current_period_end == date(2026, 9, 27)


def test_ownership_constraint_and_category_delete_remove_only_owned_budget() -> None:
    owner_id = create_user("cascade-owner@example.com")
    other_id = create_user("cascade-other@example.com")
    owned_category = create_category(owner_id, "Owned")
    foreign_category = create_category(other_id, "Foreign")
    budget_id = create_budget(
        owner_id=owner_id,
        category_id=owned_category,
        currency="COP",
        period=BudgetPeriod.MONTHLY,
    )
    foreign_budget_id = create_budget(
        owner_id=other_id,
        category_id=foreign_category,
        currency="COP",
        period=BudgetPeriod.MONTHLY,
    )

    with pytest.raises(BudgetNotFoundError):
        budget_service().update(
            owner_id=other_id,
            budget_id=budget_id,
            command=BudgetUpdateCommand(amount=Decimal("5.00")),
        )

    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency_id is not None
        session.add(
            Budget(
                user_id=owner_id,
                category_id=foreign_category,
                currency_id=currency_id,
                name="Invalid cross-owner budget",
                amount=Decimal("10.00"),
                schedule_type="recurring",
                frequency_unit="month",
                frequency_interval=1,
                starts_on=date(2026, 9, 1),
                is_active=True,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    category_service().delete(owner_id=owner_id, category_id=owned_category)

    with get_session_factory()() as session:
        assert (
            session.scalar(select(func.count()).select_from(Budget).where(Budget.id == budget_id))
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Budget).where(Budget.id == foreign_budget_id)
            )
            == 1
        )

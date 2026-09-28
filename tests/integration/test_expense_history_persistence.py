from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.config import get_settings
from finance_ai.db.models.accounts import Account
from finance_ai.db.models.catalogs import Category, IncomeSource, PaymentMethod
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.receipts import Expense, Receipt
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.transactions import AccountMovement, FinancialTransaction
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.expenses.router import get_authenticated_principal
from finance_ai.main import create_app

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")
NOW = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)


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
def clean_data(migrated_database: None) -> None:
    with get_session_factory()() as session:
        session.execute(text("TRUNCATE TABLE app_user RESTART IDENTITY CASCADE"))
        session.commit()


@pytest.fixture
def client(clean_data: None) -> TestClient:
    app = create_app()
    principal = PublicAuthenticatedUser(
        user_id=1,
        email="owner@example.com",
        preferred_currency="COP",
    )
    app.dependency_overrides[get_authenticated_principal] = lambda: principal
    with TestClient(app) as test_client:
        yield test_client


def create_user(*, email: str, currency_code: str = "COP") -> int:
    with get_session_factory()() as session:
        role_id = session.scalar(select(Role.id).where(Role.name == "user"))
        currency_id = session.scalar(select(Currency.id).where(Currency.code == currency_code))
        assert role_id is not None
        assert currency_id is not None
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


def create_account(*, user_id: int, name: str = "Cash") -> int:
    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency_id is not None
        account = Account(
            user_id=user_id,
            name=name,
            account_type="cash",
            currency_id=currency_id,
            opening_balance=Decimal("100.00"),
            is_active=True,
        )
        session.add(account)
        session.commit()
        return account.id


def create_category(*, user_id: int, name: str = "Food") -> int:
    with get_session_factory()() as session:
        category = Category(user_id=user_id, name=name, color="blue", icon="food")
        session.add(category)
        session.commit()
        return category.id


def create_expense(
    *,
    user_id: int,
    account_id: int,
    category_id: int | None,
    amount: str = "25.00",
    occurred_at: datetime = NOW,
    status: str = "confirmed",
    store: str = "Market",
) -> tuple[int, int, int]:
    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        payment_method_id = session.scalar(select(PaymentMethod.id).limit(1))
        assert currency_id is not None
        assert payment_method_id is not None
        value = Decimal(amount)
        transaction = FinancialTransaction(
            user_id=user_id,
            transaction_type="expense",
            status=status,
            occurred_at=occurred_at,
            description=f"Purchase at {store}",
            capture_source="manual",
        )
        session.add(transaction)
        session.flush()
        receipt = Receipt(
            user_id=user_id,
            transaction_id=transaction.id,
            store=store,
            date=occurred_at.date(),
            currency_id=currency_id,
            category_id=category_id,
            purchase_reason="Household purchase",
            total=value,
            payment_method_id=payment_method_id,
            image="receipt.jpg",
        )
        session.add(receipt)
        session.flush()
        expense = Expense(
            user_id=user_id,
            receipt_id=receipt.id,
            category_id=category_id,
            subcategory_id=None,
            position=1,
            name="Groceries",
            quantity=Decimal("1"),
            unit_price=value,
            amount=value,
            note="Weekly food",
            image_path="expense.jpg",
        )
        session.add(expense)
        session.add(
            AccountMovement(
                user_id=user_id,
                transaction_id=transaction.id,
                account_id=account_id,
                amount=-value,
            )
        )
        session.commit()
        return expense.id, receipt.id, transaction.id


def create_history_transaction(
    *,
    user_id: int,
    transaction_type: str,
    account_id: int,
    occurred_at: datetime,
    amount: str,
    second_account_id: int | None = None,
) -> int:
    with get_session_factory()() as session:
        income_source_id: int | None = None
        if transaction_type == "income":
            income_source = IncomeSource(user_id=user_id, name="Salary", is_active=True)
            session.add(income_source)
            session.flush()
            income_source_id = income_source.id
        transaction = FinancialTransaction(
            user_id=user_id,
            transaction_type=transaction_type,
            status="confirmed",
            occurred_at=occurred_at,
            description=f"{transaction_type.title()} entry",
            capture_source="manual",
            income_source_id=income_source_id,
        )
        session.add(transaction)
        session.flush()
        value = Decimal(amount)
        movements = [
            AccountMovement(
                user_id=user_id,
                transaction_id=transaction.id,
                account_id=account_id,
                amount=value if transaction_type == "income" else -value,
            )
        ]
        if second_account_id is not None:
            movements.append(
                AccountMovement(
                    user_id=user_id,
                    transaction_id=transaction.id,
                    account_id=second_account_id,
                    amount=value,
                )
            )
        session.add_all(movements)
        session.commit()
        return transaction.id


def test_expense_read_update_recalculates_total_and_movement(client: TestClient) -> None:
    owner_id = create_user(email="owner@example.com")
    account_id = create_account(user_id=owner_id)
    category_id = create_category(user_id=owner_id)
    expense_id, receipt_id, transaction_id = create_expense(
        user_id=owner_id,
        account_id=account_id,
        category_id=category_id,
    )

    listed = client.get("/api/v1/expenses")
    detail = client.get(f"/api/v1/expenses/{expense_id}")
    updated = client.patch(
        f"/api/v1/expenses/{expense_id}",
        json={
            "amount": "30.00",
            "name": "Updated groceries",
            "note": None,
            "category_id": category_id,
            "account_id": account_id,
            "occurred_at": "2026-09-18T12:30:00Z",
            "receipt_name": "Updated market",
            "receipt_note": "Updated receipt",
            "description": "Updated purchase",
        },
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == expense_id
    assert detail.status_code == 200
    assert detail.json()["currency_code"] == "COP"
    assert detail.json()["receipt_image"] == "receipt.jpg"
    assert detail.json()["image_path"] == "expense.jpg"
    assert updated.status_code == 200
    assert updated.json()["amount"] == "30.00"
    assert updated.json()["name"] == "Updated groceries"
    assert updated.json()["note"] is None
    assert updated.json()["receipt_name"] == "Updated market"
    assert updated.json()["receipt_note"] == "Updated receipt"
    assert updated.json()["description"] == "Updated purchase"

    with get_session_factory()() as session:
        receipt = session.get(Receipt, receipt_id)
        movement = session.scalar(
            select(AccountMovement).where(AccountMovement.transaction_id == transaction_id)
        )
        item = session.get(Expense, expense_id)
        assert receipt is not None and receipt.total == Decimal("30.00")
        assert receipt.store == "Updated market"
        assert receipt.purchase_reason == "Updated receipt"
        assert receipt.date.isoformat() == "2026-09-18"
        assert receipt.category_id == category_id
        assert movement is not None and movement.amount == Decimal("-30.00")
        assert item is not None and item.note is None


def _create_receipt_expense_with_second_line(
    *, owner_id: int, account_id: int, category_id: int
) -> tuple[int, int, int]:
    expense_id, receipt_id, transaction_id = create_expense(
        user_id=owner_id,
        account_id=account_id,
        category_id=category_id,
    )
    with get_session_factory()() as session:
        session.add(
            Expense(
                user_id=owner_id,
                receipt_id=receipt_id,
                category_id=category_id,
                position=2,
                name="Second line",
                quantity=Decimal("1"),
                unit_price=Decimal("5.00"),
                amount=Decimal("5.00"),
            )
        )
        session.commit()
    return expense_id, receipt_id, transaction_id


def test_expense_item_replacement_updates_lines_total_and_movement(
    client: TestClient,
) -> None:
    owner_id = create_user(email="owner@example.com")
    account_id = create_account(user_id=owner_id)
    category_id = create_category(user_id=owner_id)
    expense_id, receipt_id, transaction_id = _create_receipt_expense_with_second_line(
        owner_id=owner_id, account_id=account_id, category_id=category_id
    )

    replaced = client.patch(
        f"/api/v1/expenses/{expense_id}",
        json={
            "items": [
                {
                    "name": "Cafe",
                    "total_price": "40.00",
                    "quantity": "2",
                    "unit_price": "20.00",
                    "category_id": category_id,
                },
                {"name": "Pan", "total_price": "10.00"},
            ]
        },
    )

    assert replaced.status_code == 200
    body = replaced.json()
    assert body["id"] == expense_id
    assert [item["name"] for item in body["items"]] == ["Cafe", "Pan"]
    assert [item["amount"] for item in body["items"]] == ["40.00", "10.00"]
    assert [item["category"]["id"] for item in body["items"]] == [category_id, category_id]
    assert body["receipt_total"] == "50.00"

    with get_session_factory()() as session:
        receipt = session.get(Receipt, receipt_id)
        movement = session.scalar(
            select(AccountMovement).where(AccountMovement.transaction_id == transaction_id)
        )
        rows = session.scalars(
            select(Expense).where(Expense.receipt_id == receipt_id).order_by(Expense.position)
        ).all()
        assert receipt is not None and receipt.total == Decimal("50.00")
        assert movement is not None and movement.amount == Decimal("-50.00")
        assert [row.name for row in rows] == ["Cafe", "Pan"]
        assert [row.position for row in rows] == [1, 2]
        assert rows[0].id == expense_id


def test_expense_item_replacement_collapses_and_rejects_foreign_categories(
    client: TestClient,
) -> None:
    owner_id = create_user(email="owner@example.com")
    account_id = create_account(user_id=owner_id)
    category_id = create_category(user_id=owner_id)
    expense_id, receipt_id, transaction_id = _create_receipt_expense_with_second_line(
        owner_id=owner_id, account_id=account_id, category_id=category_id
    )
    foreign_owner = create_user(email="other@example.com")
    foreign_category = create_category(user_id=foreign_owner, name="Foreign")

    rejected = client.patch(
        f"/api/v1/expenses/{expense_id}",
        json={"items": [{"name": "Cafe", "total_price": "10.00", "category_id": foreign_category}]},
    )
    collapsed = client.patch(
        f"/api/v1/expenses/{expense_id}",
        json={"items": [], "amount": "75.00"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "expense_category_not_found"
    assert collapsed.status_code == 200
    body = collapsed.json()
    assert [item["name"] for item in body["items"]] == ["Market"]
    assert body["receipt_total"] == "75.00"

    with get_session_factory()() as session:
        receipt = session.get(Receipt, receipt_id)
        movement = session.scalar(
            select(AccountMovement).where(AccountMovement.transaction_id == transaction_id)
        )
        assert receipt is not None and receipt.total == Decimal("75.00")
        assert movement is not None and movement.amount == Decimal("-75.00")


def test_expense_ownership_editability_and_atomic_delete(client: TestClient) -> None:
    owner_id = create_user(email="owner@example.com")
    other_id = create_user(email="other@example.com")
    owner_account = create_account(user_id=owner_id)
    other_account = create_account(user_id=other_id, name="Other cash")
    category_id = create_category(user_id=owner_id)
    foreign_expense_id, _, _ = create_expense(
        user_id=other_id,
        account_id=other_account,
        category_id=None,
    )
    pending_expense_id, _, _ = create_expense(
        user_id=owner_id,
        account_id=owner_account,
        category_id=category_id,
        status="pending",
    )
    own_expense_id, receipt_id, transaction_id = create_expense(
        user_id=owner_id,
        account_id=owner_account,
        category_id=category_id,
    )

    assert client.get(f"/api/v1/expenses/{foreign_expense_id}").status_code == 404
    assert (
        client.patch(f"/api/v1/expenses/{foreign_expense_id}", json={"amount": "2.00"}).status_code
        == 404
    )
    assert client.delete(f"/api/v1/expenses/{foreign_expense_id}").status_code == 404
    assert (
        client.patch(f"/api/v1/expenses/{pending_expense_id}", json={"amount": "2.00"}).status_code
        == 409
    )
    assert client.delete(f"/api/v1/expenses/{pending_expense_id}").status_code == 409

    deleted = client.delete(f"/api/v1/expenses/{own_expense_id}")

    assert deleted.status_code == 204
    with get_session_factory()() as session:
        assert session.get(Expense, own_expense_id) is None
        assert session.get(Receipt, receipt_id) is None
        assert session.get(FinancialTransaction, transaction_id) is None
        assert (
            session.scalar(
                select(AccountMovement.id).where(AccountMovement.transaction_id == transaction_id)
            )
            is None
        )


def test_history_supports_types_filters_search_currency_and_pagination(client: TestClient) -> None:
    owner_id = create_user(email="owner@example.com")
    other_id = create_user(email="other@example.com")
    account_id = create_account(user_id=owner_id)
    second_account_id = create_account(user_id=owner_id, name="Bank")
    category_id = create_category(user_id=owner_id)
    create_expense(
        user_id=owner_id,
        account_id=account_id,
        category_id=category_id,
        occurred_at=datetime(2026, 9, 10, 9, tzinfo=UTC),
    )
    income_id = create_history_transaction(
        user_id=owner_id,
        transaction_type="income",
        account_id=account_id,
        occurred_at=datetime(2026, 9, 11, 9, tzinfo=UTC),
        amount="100.00",
    )
    transfer_id = create_history_transaction(
        user_id=owner_id,
        transaction_type="transfer",
        account_id=account_id,
        second_account_id=second_account_id,
        occurred_at=datetime(2026, 9, 12, 9, tzinfo=UTC),
        amount="20.00",
    )
    create_expense(
        user_id=other_id,
        account_id=create_account(user_id=other_id, name="Other bank"),
        category_id=None,
        occurred_at=datetime(2026, 9, 20, 9, tzinfo=UTC),
    )

    page = client.get("/api/v1/history", params={"page": 1, "page_size": 2})
    income = client.get("/api/v1/history", params={"type": "income", "search": " salary "})
    category = client.get(
        "/api/v1/history",
        params={
            "category_id": category_id,
            "period": "custom",
            "start_date": "2026-09-10",
            "end_date": "2026-09-12",
        },
    )
    account = client.get("/api/v1/history", params={"account_id": account_id})
    expense_history = client.get("/api/v1/history", params={"type": "expense"})

    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert page.json()["has_next"] is True
    assert [item["id"] for item in page.json()["items"]] == [transfer_id, income_id]
    assert page.json()["items"][0]["movements"][0]["currency_code"] == "COP"
    assert income.status_code == 200
    assert income.json()["items"][0]["type"] == "income"
    assert income.json()["items"][0]["income_source"] == "Salary"
    assert category.status_code == 200
    assert category.json()["total"] == 1
    assert category.json()["items"][0]["type"] == "expense"
    assert expense_history.status_code == 200
    expense_details = expense_history.json()["items"][0]["expense"]
    assert expense_details["receipt_image"] == "receipt.jpg"
    assert expense_details["items"][0]["image_path"] == "expense.jpg"
    assert account.status_code == 200
    assert account.json()["total"] == 3

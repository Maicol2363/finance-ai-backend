from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from finance_ai.categories.policy import CategoryPolicy
from finance_ai.categories.schemas import (
    CategoryColor,
    CategoryCreateCommand,
    CategoryIcon,
    CategoryUpdateCommand,
)
from finance_ai.categories.service import CategoryService
from finance_ai.core.config import get_settings
from finance_ai.core.errors import (
    CategoryNameConflictError,
    CategoryNotFoundError,
    CategoryOrderInvalidError,
)
from finance_ai.db.category_uow import SQLAlchemyCategoryUnitOfWork
from finance_ai.db.models.catalogs import Category
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


def require_safe_test_database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
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


def service() -> CategoryService:
    return CategoryService(
        policy=CategoryPolicy(),
        uow_factory=lambda: SQLAlchemyCategoryUnitOfWork(get_session_factory()),
    )


def create_user(email: str) -> int:
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


def create_category(owner_id: int, name: str) -> int:
    return (
        service()
        .create(
            owner_id=owner_id,
            command=CategoryCreateCommand(
                name=name, color=CategoryColor.BLUE, icon=CategoryIcon.OTHER
            ),
        )
        .id
    )


def test_schema_and_crud_preserve_owner_and_gap_free_order() -> None:
    first_owner = create_user("first@example.com")
    second_owner = create_user("second@example.com")
    first = create_category(first_owner, "Needs")
    second = create_category(first_owner, "Wants")
    foreign = create_category(second_owner, "Needs")

    result = service().update(
        owner_id=first_owner,
        category_id=second,
        command=CategoryUpdateCommand(color=CategoryColor.PURPLE, icon=CategoryIcon.WANTS),
    )
    assert result.color is CategoryColor.PURPLE
    service().reorder(owner_id=first_owner, category_ids=[second, first])
    service().delete(owner_id=first_owner, category_id=second)

    assert [
        (item.id, item.sort_order) for item in service().list(owner_id=first_owner).categories
    ] == [(first, 0)]
    assert service().get(owner_id=second_owner, category_id=foreign).name == "Needs"
    with pytest.raises(CategoryNotFoundError):
        service().get(owner_id=second_owner, category_id=first)


def test_invalid_reorder_rolls_back_without_partial_changes() -> None:
    owner_id = create_user("person@example.com")
    first = create_category(owner_id, "One")
    second = create_category(owner_id, "Two")

    with pytest.raises(CategoryOrderInvalidError):
        service().reorder(owner_id=owner_id, category_ids=[second])

    assert [item.id for item in service().list(owner_id=owner_id).categories] == [first, second]


def test_deleted_name_is_reusable_and_database_rejects_concurrent_duplicates() -> None:
    owner_id = create_user("person@example.com")
    category_id = create_category(owner_id, "Travel")
    service().delete(owner_id=owner_id, category_id=category_id)
    assert create_category(owner_id, "travel") > category_id

    def attempt(name: str) -> str:
        try:
            create_category(owner_id, name)
        except CategoryNameConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ["Food", "food"]))
    assert sorted(outcomes) == ["conflict", "created"]
    with get_session_factory()() as session:
        matches = session.scalars(
            select(Category).where(Category.user_id == owner_id, text("lower(name) = 'food'"))
        ).all()
    assert len(matches) == 1


def test_category_constraints_and_indexes_are_installed() -> None:
    with get_session_factory()() as session:
        constraints = set(
            session.scalars(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'category'::regclass AND contype = 'c'"
                )
            )
        )
        indexes = set(
            session.scalars(text("SELECT indexname FROM pg_indexes WHERE tablename = 'category'"))
        )
    assert constraints == {
        "ck_category_color_supported",
        "ck_category_icon_supported",
        "ck_category_name_trimmed",
        "ck_category_sort_order_non_negative",
    }
    assert {"uq_category_user_name", "ix_category_user_sort"} <= indexes

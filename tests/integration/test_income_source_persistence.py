import os
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from finance_ai.core.config import get_settings
from finance_ai.core.errors import IncomeSourceNameTakenError, IncomeSourceNotFoundError
from finance_ai.db.models.catalogs import IncomeSource
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.db.uow import SQLAlchemyIncomeSourceUnitOfWork
from finance_ai.income_sources.service import IncomeSourceService

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


def require_safe_test_database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    database_name = make_url(TEST_DATABASE_URL).database or ""
    if not database_name.endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session", autouse=True)
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
def clean_income_source_data(migrated_database: None) -> None:
    with get_session_factory()() as session:
        session.execute(
            text("TRUNCATE TABLE income_source, category, app_user RESTART IDENTITY CASCADE")
        )
        session.commit()


def income_source_service() -> IncomeSourceService:
    def uow_factory() -> SQLAlchemyIncomeSourceUnitOfWork:
        return SQLAlchemyIncomeSourceUnitOfWork(get_session_factory())

    return IncomeSourceService(uow_factory=uow_factory)


def create_user(email: str) -> int:
    with get_session_factory()() as session:
        currency_id = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        user = AppUser(
            email=email,
            password_hash="x" * 20,
            preferred_currency_id=currency_id,
            is_active=True,
        )
        session.add(user)
        session.commit()
        return user.id


def test_owned_crud_unique_index_and_isolation() -> None:
    first_user = create_user("first@example.com")
    second_user = create_user("second@example.com")
    service = income_source_service()

    created = service.create(user_id=first_user, name="  Salary  ")
    assert created.name == "Salary"
    assert [item.name for item in service.list(user_id=first_user)] == ["Salary"]
    assert service.list(user_id=second_user) == []

    with pytest.raises(IncomeSourceNameTakenError):
        service.create(user_id=first_user, name="salary")
    # Another user's catalog is unaffected: the same name is allowed for them.
    assert service.create(user_id=second_user, name="salary").name == "salary"

    renamed = service.rename(user_id=first_user, income_source_id=created.id, name="Freelance")
    assert renamed.name == "Freelance"
    with pytest.raises(IncomeSourceNotFoundError):
        service.rename(user_id=second_user, income_source_id=created.id, name="Stolen")

    service.delete(user_id=first_user, income_source_id=created.id)
    assert service.list(user_id=first_user) == []
    assert service.create(user_id=first_user, name="Salary").name == "Salary"


def test_unique_index_enforced_at_database_level() -> None:
    user_id = create_user("constraint@example.com")
    service = income_source_service()
    service.create(user_id=user_id, name="Salary")

    with get_session_factory()() as session:
        session.add(IncomeSource(user_id=user_id, name="SALARY", is_active=True))
        with pytest.raises(Exception) as error:
            session.flush()
        assert "uq_income_source_user_name" in str(error.value)
        session.rollback()

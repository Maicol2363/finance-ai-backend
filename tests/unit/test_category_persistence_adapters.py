from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.categories.schemas import CategoryColor, CategoryIcon
from finance_ai.db.base import Base
from finance_ai.db.models.catalogs import Category
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency
from finance_ai.db.repositories.categories import SQLAlchemyCategoryRepository

NOW = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.fixture
def category_session_factory() -> sessionmaker[Session]:
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
        session.add(cop)
        session.flush()
        user = AppUser(
            id=1,
            email="user@example.com",
            password_hash="synthetic",
            preferred_currency_id=1,
            is_active=True,
        )
        session.add(user)
        session.flush()
        cat1 = Category(
            id=10,
            user_id=1,
            name="Needs",
            color=CategoryColor.BLUE.value,
            icon=CategoryIcon.ESSENTIALS.value,
            sort_order=0,
        )
        cat2 = Category(
            id=20,
            user_id=1,
            name="Wants",
            color=CategoryColor.PURPLE.value,
            icon=CategoryIcon.WANTS.value,
            sort_order=1,
        )
        cat3 = Category(
            id=30,
            user_id=1,
            name="Savings",
            color=CategoryColor.GREEN.value,
            icon=CategoryIcon.OTHER.value,
            sort_order=2,
        )
        session.add_all((cat1, cat2, cat3))
        session.commit()
    yield factory
    engine.dispose()


def test_sqlalchemy_category_repository_reorder(
    category_session_factory: sessionmaker[Session],
) -> None:
    with category_session_factory() as session:
        repo = SQLAlchemyCategoryRepository(session)

        # Check initial order
        records = repo.list_owned(owner_id=1)
        assert [r.id for r in records] == [10, 20, 30]

        # Reorder to 30, 10, 20
        repo.reorder(owner_id=1, category_ids=(30, 10, 20))
        session.flush()

        # Check reordered order
        records_after = repo.list_owned(owner_id=1)
        assert [r.id for r in records_after] == [30, 10, 20]
        assert [r.sort_order for r in records_after] == [0, 1, 2]

        # Empty reorder does nothing and does not crash
        repo.reorder(owner_id=1, category_ids=())

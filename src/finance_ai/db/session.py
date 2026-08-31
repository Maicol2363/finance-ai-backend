from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=1_800,
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def get_db_session() -> Generator[Session]:
    with get_session_factory()() as session:
        yield session


def clear_session_caches() -> None:
    get_session_factory.cache_clear()
    engine = get_engine.cache_info()
    if engine.currsize:
        get_engine().dispose()
    get_engine.cache_clear()

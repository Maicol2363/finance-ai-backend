import os
from collections.abc import Iterator

# Must be set before any application modules are imported during pytest collection
os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
# Pinned (not just removed): a developer .env may select another provider, and an
# environment variable wins over the .env file, so tests never depend on local settings.
os.environ["FINANCE_AI_LLM_PROVIDER"] = "gemini"

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from finance_ai.capture.models import TextCaptureProposalModel


@pytest.fixture
def drafts_engine() -> Iterator[Engine]:
    """In-memory database holding only the HU-013 draft table."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TextCaptureProposalModel.__table__.create(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(drafts_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=drafts_engine, autoflush=False, expire_on_commit=False)

"""PostgreSQL claim atomicity checks for SUP-01."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.core.errors import (
    SupportTicketClaimConflictError,
    SupportTicketNotFoundError,
)
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.support import SupportTicket, SupportTicketActivity
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.service import SupportTicketService

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def migrated_test_database() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    os.environ["FINANCE_AI_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    from finance_ai.core.config import get_settings

    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_support_audit_events(migrated_test_database: None):
    yield
    with get_session_factory()() as session:
        # Migration fixtures downgrade this isolated database between modules.
        session.execute(text("TRUNCATE TABLE security_audit_event"))
        session.commit()


def _agent(session, role_id: int, currency_id: int) -> AppUser:
    user = AppUser(
        email=f"agent-{uuid4()}@example.test",
        password_hash="not-used",
        role_id=role_id,
        preferred_currency_id=currency_id,
    )
    session.add(user)
    session.flush()
    return user


def test_two_concurrent_claims_have_one_winner_and_audit_evidence(
    migrated_test_database: None,
) -> None:
    with get_session_factory()() as session:
        session.execute(
            text("TRUNCATE TABLE security_audit_event, app_user RESTART IDENTITY CASCADE")
        )
        role = session.scalar(select(Role).where(Role.name == "support_agent"))
        currency = session.scalar(select(Currency).where(Currency.code == "COP"))
        assert role is not None and currency is not None
        first = _agent(session, role.id, currency.id)
        second = _agent(session, role.id, currency.id)
        ticket = SupportTicket(
            customer_user_id=first.id,
            created_by_user_id=first.id,
            subject="Consulta de prueba",
            category="general",
            description="Contenido privado de prueba",
        )
        session.add(ticket)
        session.flush()
        ticket_id = ticket.id
        first_id, second_id = first.id, second.id
        session.commit()

    service = SupportTicketService(get_session_factory(), SecurityAuditWriter())
    gate = Barrier(2)

    def attempt(agent_id: int):
        user = StaffSessionUser(id=agent_id, email="agent@example.test", role="support_agent")
        gate.wait(timeout=10)
        try:
            return service.claim(user, ticket_id, f"claim-{agent_id}")
        except SupportTicketClaimConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [first_id, second_id]))
    successes = [result for result in outcomes if not isinstance(result, Exception)]
    conflicts = [
        result for result in outcomes if isinstance(result, SupportTicketClaimConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    winning_id = successes[0].assigned_staff_user_id
    assert winning_id is not None
    losing_id = second_id if winning_id == first_id else first_id
    winning_agent = StaffSessionUser(
        id=winning_id, email="winner@example.test", role="support_agent"
    )
    losing_agent = StaffSessionUser(id=losing_id, email="loser@example.test", role="support_agent")
    details = service.get_assigned(winning_agent, ticket_id).model_dump()
    assert details["description"] == "Contenido privado de prueba"
    assert not {"customer_user_id", "balance", "transactions", "credentials"} & set(details)
    with pytest.raises(SupportTicketNotFoundError):
        service.get_assigned(losing_agent, ticket_id)
    with get_session_factory()() as session:
        claimed = session.get(SupportTicket, ticket_id)
        activities = list(
            session.scalars(
                select(SupportTicketActivity).where(SupportTicketActivity.ticket_id == ticket_id)
            )
        )
        audit = list(
            session.scalars(
                select(SecurityAuditEvent).where(
                    SecurityAuditEvent.event_type == "support.ticket_claimed"
                )
            )
        )
        assert claimed is not None
        assert claimed.assigned_staff_user_id in {first_id, second_id}
        assert claimed.status == "in_progress"
        assert len(activities) == 1
        assert activities[0].actor_user_id == claimed.assigned_staff_user_id
        assert len(audit) == 1
        assert audit[0].target_resource_id == str(ticket_id)
        assert audit[0].actor_id == str(claimed.assigned_staff_user_id)
        assert audit[0].context == {}

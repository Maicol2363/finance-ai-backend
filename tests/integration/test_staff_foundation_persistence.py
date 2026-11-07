"""PostgreSQL persistence checks for the staff foundation schema."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from finance_ai.core.config import get_settings
from finance_ai.db.models.admin import AdminTotpFactor, StaffInvitation
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import UserSession
from finance_ai.db.models.support import (
    SupportTicket,
    SupportTicketActivity,
    SupportTicketMessage,
)
from finance_ai.db.seeds import ROLE_SEEDS, seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    os.environ["FINANCE_AI_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_staff_rows(migrated_database: None) -> None:
    with get_session_factory()() as session:
        session.execute(
            text("TRUNCATE TABLE security_audit_event, app_user RESTART IDENTITY CASCADE")
        )
        session.commit()
    yield
    with get_session_factory()() as session:
        # This fixture uses an isolated _test database whose migration fixtures downgrade to base.
        session.execute(text("TRUNCATE TABLE security_audit_event"))
        session.commit()


def create_user(session, role_name: str) -> AppUser:
    role = session.scalar(select(Role).where(Role.name == role_name))
    currency = session.scalar(select(Currency).where(Currency.code == "COP"))
    assert role is not None
    assert currency is not None
    user = AppUser(
        email=f"{role_name}-{uuid4()}@example.test",
        password_hash="$argon2id$synthetic",
        role_id=role.id,
        preferred_currency_id=currency.id,
    )
    session.add(user)
    session.flush()
    return user


def test_role_seed_is_exact_and_repeatable() -> None:
    with get_session_factory()() as session:
        names = set(session.scalars(select(Role.name)).all())

    assert names == set(ROLE_SEEDS)
    assert names == {"user", "support_agent", "admin", "security_auditor"}


def test_ticket_messages_and_activity_preserve_separate_visibility_fields() -> None:
    with get_session_factory()() as session:
        customer = create_user(session, "user")
        agent = create_user(session, "support_agent")
        administrator = create_user(session, "admin")
        session.add_all(
            [
                SupportTicket(
                    customer_user_id=customer.id,
                    created_by_user_id=customer.id,
                    subject="Access issue",
                    category="account",
                    description="Cannot reach the account settings.",
                    status="escalated",
                    assigned_staff_user_id=administrator.id,
                    originating_support_agent_id=agent.id,
                )
            ]
        )
        session.flush()
        ticket = session.scalar(select(SupportTicket))
        assert ticket is not None
        session.add_all(
            [
                SupportTicketMessage(
                    ticket_id=ticket.id,
                    author_user_id=customer.id,
                    body="I need help with account settings.",
                ),
                SupportTicketActivity(
                    ticket_id=ticket.id,
                    actor_user_id=agent.id,
                    event_type="ticket.escalated",
                    previous_status="in_progress",
                    new_status="escalated",
                    previous_assignee_user_id=agent.id,
                    new_assignee_user_id=administrator.id,
                    internal_reason="Requires Administrator access.",
                ),
            ]
        )
        session.commit()
        ticket_id = ticket.id

    with get_session_factory()() as session:
        message = session.scalar(
            select(SupportTicketMessage).where(SupportTicketMessage.ticket_id == ticket_id)
        )
        activity = session.scalar(
            select(SupportTicketActivity).where(SupportTicketActivity.ticket_id == ticket_id)
        )
        assert message is not None and message.body == "I need help with account settings."
        assert activity is not None and activity.internal_reason == "Requires Administrator access."


def test_invitation_totp_and_mfa_session_state_persist() -> None:
    with get_session_factory()() as session:
        administrator = create_user(session, "admin")
        agent_role = session.scalar(select(Role).where(Role.name == "support_agent"))
        assert agent_role is not None
        now = datetime.now(UTC)
        invite = StaffInvitation(
            email="new-agent@example.test",
            role_id=agent_role.id,
            token_hash="a" * 64,
            created_by_admin_user_id=administrator.id,
            expires_at=now + timedelta(hours=1),
        )
        factor = AdminTotpFactor(
            admin_user_id=administrator.id,
            secret_ciphertext=b"encrypted-secret",
            state="active",
            activated_at=now,
            last_accepted_time_step=100,
        )
        staff_session = UserSession(
            user_id=administrator.id,
            access_token_hash="b" * 64,
            platform="web",
            access_expires_at=now + timedelta(minutes=15),
            idle_expires_at=now + timedelta(days=30),
            absolute_expires_at=now + timedelta(days=90),
            last_renewed_at=now,
            mfa_verified_at=now,
        )
        session.add_all([invite, factor, staff_session])
        session.commit()
        invite_id, factor_id, staff_session_id = invite.id, factor.id, staff_session.id

    with get_session_factory()() as session:
        assert session.get(StaffInvitation, invite_id).token_hash == "a" * 64
        assert session.get(AdminTotpFactor, factor_id).last_accepted_time_step == 100
        assert session.get(UserSession, staff_session_id).mfa_verified_at is not None


def test_audit_events_are_queryable_and_reject_update_and_delete() -> None:
    with get_session_factory()() as session:
        actor = create_user(session, "admin")
        event = SecurityAuditEvent(
            event_type="staff.activated",
            actor_id=str(actor.id),
            target_resource_type="staff_account",
            target_resource_id="42",
            result="success",
            correlation_id="request-123",
            context={"role": "support_agent"},
        )
        session.add(event)
        session.commit()
        event_id = event.event_id
        event_id_text = str(event_id)

    with get_session_factory()() as session:
        persisted = session.get(SecurityAuditEvent, event_id)
        assert persisted is not None
        assert persisted.event_type == "staff.activated"
        assert persisted.context == {"role": "support_agent"}
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM security_audit_event WHERE event_type = 'staff.activated'"
                )
            )
            == 1
        )
        for statement in (
            text("UPDATE security_audit_event SET result = 'changed' WHERE event_id = :event_id"),
            text("DELETE FROM security_audit_event WHERE event_id = :event_id"),
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                session.execute(statement, {"event_id": event_id_text})
                session.flush()
            session.rollback()

        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with pytest.raises(RuntimeError, match="Refusing to drop security_audit_event"):
            alembic_command.downgrade(config, "0017_text_capture_idempotency")
        assert session.get(SecurityAuditEvent, event_id) is not None

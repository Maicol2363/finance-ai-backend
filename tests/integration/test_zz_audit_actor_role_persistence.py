"""PostgreSQL checks for the event-time actor role (0021) and customer sign-in evidence."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url

from finance_ai.audit.policy import AuditFilters, normalize_period
from finance_ai.audit.query_service import AuditQueryService
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.session_policy import SessionPolicy, SystemClock
from finance_ai.auth.session_schemas import LoginCommand
from finance_ai.auth.session_service import SessionService
from finance_ai.core.config import get_settings
from finance_ai.core.errors import InvalidCredentialsError
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.db.session_uow import SQLAlchemySessionUnitOfWork

pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


class IntegrationPasswordHasher:
    def verify_real_or_dummy(self, password_hash: str | None, candidate: str) -> bool:
        return password_hash == "integration-hash" and candidate == "correct-password"

    def needs_rehash(self, password_hash: str) -> bool:
        del password_hash
        return False

    def hash(self, password: str) -> str:
        raise AssertionError(f"unexpected rehash of {len(password)} characters")


@pytest.fixture(scope="module", autouse=True)
def migrated_test_database() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    os.environ["FINANCE_AI_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.upgrade(config, "head")
    # Exercise the 0021 downgrade/upgrade pair on the isolated test database.
    alembic_command.downgrade(config, "0020_admin_account_management")
    columns = inspect(get_session_factory().kw["bind"]).get_columns("security_audit_event")
    assert "actor_role" not in {column["name"] for column in columns}
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_rows(migrated_test_database: None) -> Iterator[None]:
    with get_session_factory()() as session:
        session.execute(
            text("TRUNCATE TABLE security_audit_event, app_user RESTART IDENTITY CASCADE")
        )
        session.commit()
    try:
        yield
    finally:
        with get_session_factory()() as session:
            session.execute(text("TRUNCATE TABLE security_audit_event"))
            session.commit()


def create_account(role: str, *, active: bool = True) -> tuple[int, str]:
    with get_session_factory()() as session:
        roles = dict(session.execute(select(Role.name, Role.id)).all())
        currency = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency is not None
        account = AppUser(
            email=f"{role}-{uuid4()}@example.test",
            password_hash="integration-hash",
            role_id=roles[role],
            preferred_currency_id=currency,
            is_active=active,
        )
        session.add(account)
        session.commit()
        return account.id, account.email


def append(actor_id: str | None, *, event_type: str = "staff.login") -> None:
    with get_session_factory()() as session, session.begin():
        SecurityAuditWriter().append(
            session,
            event_type=event_type,
            actor_id=actor_id,
            result="success",
            correlation_id="role-test",
            context={},
        )


def roles_by_actor() -> dict[str | None, str | None]:
    with get_session_factory()() as session:
        rows = session.execute(select(SecurityAuditEvent.actor_id, SecurityAuditEvent.actor_role))
        return {actor: role for actor, role in rows}


def session_service() -> SessionService:
    return SessionService(
        password_hasher=IntegrationPasswordHasher(),  # type: ignore[arg-type]
        credential_codec=CredentialCodec(),
        policy=SessionPolicy(
            access_lifetime=timedelta(minutes=15),
            renewable_idle_lifetime=timedelta(days=30),
            absolute_lifetime=timedelta(days=90),
            max_active_sessions=10,
            terminal_retention=timedelta(days=30),
        ),
        clock=SystemClock(),
        uow_factory=lambda: SQLAlchemySessionUnitOfWork(get_session_factory()),
    )


def test_postgres_derives_the_actor_role_at_event_time_for_every_writer() -> None:
    agent, _ = create_account("support_agent")
    customer, _ = create_account("user")
    admin, _ = create_account("admin")
    auditor, _ = create_account("security_auditor")
    for actor in (agent, customer, admin, auditor):
        append(str(actor))
    append(None)
    append("999999")  # no such account

    assert roles_by_actor() == {
        str(agent): "support_agent",
        str(customer): "user",
        str(admin): "admin",
        str(auditor): "security_auditor",
        None: None,
        "999999": None,
    }


def test_postgres_ignores_a_supplied_role_and_never_rewrites_history() -> None:
    customer, _ = create_account("user")
    with get_session_factory()() as session, session.begin():
        session.add(
            SecurityAuditEvent(
                event_type="staff.login",
                actor_id=str(customer),
                actor_role="admin",
                result="success",
                correlation_id="spoof",
                context={},
            )
        )
    with get_session_factory()() as session, session.begin():
        session.execute(
            text(
                "UPDATE app_user SET role_id = (SELECT id FROM role WHERE name = 'support_agent') "
                "WHERE id = :id"
            ),
            {"id": customer},
        )
    append(str(customer))

    with get_session_factory()() as session:
        roles = list(
            session.scalars(
                select(SecurityAuditEvent.actor_role).order_by(SecurityAuditEvent.occurred_at)
            )
        )
    # The spoofed "admin" became the real role; the later event shows the new role.
    assert roles == ["user", "support_agent"]


def test_postgres_role_filter_reads_the_recorded_role() -> None:
    agent, _ = create_account("support_agent")
    customer, _ = create_account("user")
    append(str(agent))
    append(str(customer))
    service = AuditQueryService(
        get_session_factory(), SystemClock(), max_query_days=90, max_report_events=2_000
    )
    today = SystemClock().now().date()
    period = normalize_period(today - timedelta(days=1), today, "UTC", max_days=90)

    page = service.list_events(period, AuditFilters(actor_role="user"), cursor=None, limit=25)

    assert [(item.actor_id, item.actor_role) for item in page.items] == [(str(customer), "user")]


def test_postgres_customer_app_sign_in_writes_sec02_evidence() -> None:
    customer, email = create_account("user")
    inactive, inactive_email = create_account("user", active=False)
    service = session_service()

    issued = service.login(
        LoginCommand(email=email, password="correct-password", platform="android"),
        correlation_id="app-1",
    )
    for attempt_email, password in (
        ("nobody@example.test", "correct-password"),
        (email, "wrong-password"),
        (inactive_email, "correct-password"),
    ):
        with pytest.raises(InvalidCredentialsError):
            service.login(
                LoginCommand(email=attempt_email, password=password, platform="windows"),
                correlation_id="app-2",
            )
    service.logout(issued.renewable_credential, correlation_id="app-3")

    with get_session_factory()() as session:
        events = session.scalars(
            select(SecurityAuditEvent).order_by(SecurityAuditEvent.occurred_at)
        ).all()
    observed = [
        (
            event.event_type,
            event.result,
            event.actor_id,
            event.actor_role,
            event.target_resource_type,
            event.context.get("reason"),
            event.context.get("platform"),
        )
        for event in events
    ]
    assert observed == [
        ("auth.login_success", "success", str(customer), "user", "account", None, "android"),
        ("auth.login_failed", "failure", None, None, None, "invalid_credentials", "windows"),
        (
            "auth.login_failed",
            "failure",
            str(customer),
            "user",
            "account",
            "invalid_credentials",
            "windows",
        ),
        (
            "auth.login_failed",
            "denied",
            str(inactive),
            "user",
            "account",
            "account_inactive",
            "windows",
        ),
        ("auth.logout", "success", str(customer), "user", "account", None, "android"),
    ]
    assert all(event.context.get("channel") == "customer_app" for event in events)
    assert email not in str([event.context for event in events])

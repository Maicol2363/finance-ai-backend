"""PostgreSQL transaction checks for staff browser-session persistence."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.session_policy import SystemClock
from finance_ai.core.config import get_settings
from finance_ai.core.errors import AuditUnavailableError, StaffSessionInvalidError
from finance_ai.db.models.admin import StaffInvitation
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import RenewableCredential, UserSession
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.main import create_app
from finance_ai.staff_auth.dependencies import staff_cookie_names
from finance_ai.staff_auth.schemas import StaffLoginCommand
from finance_ai.staff_auth.service import StaffSessionService

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


class IntegrationPasswordHasher:
    def verify_real_or_dummy(self, password_hash: str | None, candidate: str) -> bool:
        return password_hash == "integration-hash" and candidate == "correct-password"


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
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


@pytest.fixture(autouse=True)
def clean_rows(migrated_test_database: None):
    with get_session_factory()() as session:
        session.execute(
            text("TRUNCATE TABLE security_audit_event, app_user RESTART IDENTITY CASCADE")
        )
        session.commit()
    yield


def create_authorized_service() -> tuple[StaffSessionService, str]:
    with get_session_factory()() as session:
        admin_role = session.scalar(select(Role).where(Role.name == "admin"))
        agent_role = session.scalar(select(Role).where(Role.name == "support_agent"))
        currency = session.scalar(select(Currency).where(Currency.code == "COP"))
        assert admin_role is not None and agent_role is not None and currency is not None
        administrator = AppUser(
            email=f"admin-{uuid4()}@example.test",
            password_hash="integration-hash",
            role_id=admin_role.id,
            preferred_currency_id=currency.id,
        )
        agent = AppUser(
            email=f"agent-{uuid4()}@example.test",
            password_hash="integration-hash",
            role_id=agent_role.id,
            preferred_currency_id=currency.id,
        )
        session.add_all([administrator, agent])
        session.flush()
        now = datetime.now(UTC)
        session.add(
            StaffInvitation(
                email=agent.email,
                role_id=agent_role.id,
                token_hash=uuid4().hex + uuid4().hex,
                created_by_admin_user_id=administrator.id,
                created_at=now,
                expires_at=now + timedelta(hours=1),
                consumed_at=now,
                activated_user_id=agent.id,
            )
        )
        session.commit()
        email = agent.email
    return StaffSessionService(
        session_factory=get_session_factory(),
        password_hasher=IntegrationPasswordHasher(),  # type: ignore[arg-type]
        credential_codec=CredentialCodec(),
        clock=SystemClock(),
        audit_writer=SecurityAuditWriter(),
    ), email


def test_postgres_rotation_and_replay_revoke_exact_browser_session() -> None:
    service, email = create_authorized_service()
    initial = service.login(
        StaffLoginCommand(email=email, password="correct-password"),
        correlation_id="staff-pg-login",
    )
    refreshed = service.refresh(initial[2], correlation_id="staff-pg-refresh")
    assert refreshed[2] != initial[2]
    with pytest.raises(StaffSessionInvalidError):
        service.refresh(initial[2], correlation_id="staff-pg-replay")
    with get_session_factory()() as session:
        browser_session = session.scalar(select(UserSession))
        event_types = set(session.scalars(select(SecurityAuditEvent.event_type)))
        assert browser_session is not None
        assert browser_session.platform == "staff_browser"
        assert browser_session.revocation_reason == "replay"
        assert event_types == {"staff.login", "staff.refresh", "staff.session_revoked"}


def test_postgres_refresh_audit_failure_rolls_back_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, email = create_authorized_service()
    initial = service.login(
        StaffLoginCommand(email=email, password="correct-password"),
        correlation_id="staff-pg-login",
    )
    with get_session_factory()() as session:
        before_hash = session.scalar(select(UserSession.access_token_hash))

    def fail_audit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(SecurityAuditWriter, "append", staticmethod(fail_audit))
    with pytest.raises(AuditUnavailableError):
        service.refresh(initial[2], correlation_id="staff-pg-audit-failure")
    with get_session_factory()() as session:
        browser_session = session.scalar(select(UserSession))
        credential = session.scalar(
            select(RenewableCredential).where(
                RenewableCredential.token_hash == CredentialCodec().hash_presented(initial[2])
            )
        )
        assert browser_session is not None and browser_session.access_token_hash == before_hash
        assert credential is not None and credential.consumed_at is None


def test_mobile_logout_route_cannot_revoke_staff_browser_cookie_session() -> None:
    service, email = create_authorized_service()
    issued = service.login(
        StaffLoginCommand(email=email, password="correct-password"),
        correlation_id="staff-cookie-login",
    )
    _access_cookie_name, refresh_cookie_name = staff_cookie_names()

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/auth/logout",
            json={"renewable_credential": issued[2]},
            cookies={refresh_cookie_name: issued[2]},
        )

    assert response.status_code == 204
    with get_session_factory()() as session:
        browser_session = session.scalar(
            select(UserSession).where(
                UserSession.access_token_hash == CredentialCodec().hash_presented(issued[1])
            )
        )
        assert browser_session is not None
        assert browser_session.platform == "staff_browser"
        assert browser_session.revoked_at is None

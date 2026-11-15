from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.core.errors import (
    AuditUnavailableError,
    StaffAccessDeniedError,
    StaffAuthenticationRequiredError,
    StaffLoginInvalidError,
    StaffSessionExistsError,
    StaffSessionExpiredError,
    StaffSessionInvalidError,
)
from finance_ai.db.base import Base
from finance_ai.db.models.admin import StaffInvitation
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import RenewableCredential, UserSession
from finance_ai.staff_auth.schemas import StaffLoginCommand
from finance_ai.staff_auth.service import StaffSessionService


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


class FakePasswordHasher:
    def verify_real_or_dummy(self, password_hash: str | None, candidate: str) -> bool:
        return password_hash == "password-hash" and candidate == "correct-password"


@pytest.fixture
def service_setup() -> tuple[StaffSessionService, sessionmaker[Session], dict[str, int]]:
    engine = create_engine("sqlite://")
    event.listen(
        engine,
        "connect",
        lambda connection, _: (
            connection.create_function(
                "btrim", 1, lambda value: value.strip() if value is not None else None
            ),
            connection.create_function("char_length", 1, len),
        ),
    )
    tables = [
        Role.__table__,
        Currency.__table__,
        AppUser.__table__,
        UserSession.__table__,
        RenewableCredential.__table__,
        StaffInvitation.__table__,
        SecurityAuditEvent.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as session, session.begin():
        user_role = Role(id=1, name="user")
        admin_role = Role(id=2, name="admin")
        support_role = Role(id=3, name="support_agent")
        currency = Currency(id=1, code="COP", name="Colombian peso", decimals=2, is_active=True)
        session.add_all([user_role, admin_role, support_role, currency])
        session.flush()
        admin = AppUser(
            id=1,
            email="admin@example.test",
            password_hash="password-hash",
            role_id=2,
            preferred_currency_id=1,
            is_active=True,
        )
        support = AppUser(
            id=2,
            email="agent@example.test",
            password_hash="password-hash",
            role_id=3,
            preferred_currency_id=1,
            is_active=True,
        )
        session.add_all([admin, support])
        session.flush()
        session.add(
            StaffInvitation(
                id=1,
                email=support.email,
                role_id=3,
                token_hash="a" * 64,
                created_by_admin_user_id=admin.id,
                created_at=FrozenClock().now(),
                expires_at=datetime(2027, 1, 1, tzinfo=UTC),
                consumed_at=FrozenClock().now(),
                activated_user_id=support.id,
            )
        )
    service = StaffSessionService(
        session_factory=factory,
        password_hasher=FakePasswordHasher(),  # type: ignore[arg-type]
        credential_codec=CredentialCodec(),
        clock=FrozenClock(),
        audit_writer=SecurityAuditWriter(),
    )
    return service, factory, {"admin": 1, "support": 2}


def test_login_creates_cookie_session_and_sanitized_audit_without_raw_tokens(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    user, access, refresh, access_expiry, idle_expiry, absolute_expiry = service.login(
        StaffLoginCommand(email=" AGENT@EXAMPLE.TEST ", password="correct-password"),
        correlation_id="test-correlation-1",
    )

    assert user.role == "support_agent"
    assert access_expiry < idle_expiry < absolute_expiry
    with factory() as session:
        saved = session.scalar(select(UserSession))
        renewable = session.scalar(select(RenewableCredential))
        event = session.scalar(select(SecurityAuditEvent))
        assert saved is not None and saved.platform == "staff_browser"
        assert saved.access_token_hash != access
        assert renewable is not None and renewable.token_hash != refresh
        assert event is not None and event.event_type == "staff.login"
        assert event.correlation_id == "test-correlation-1"
        assert set(event.context) == set()


def test_shared_audit_writer_uses_caller_transaction_and_rejects_unapproved_fields(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    _, factory, _ = service_setup
    writer = SecurityAuditWriter()
    with factory() as session, session.begin():
        writer.append(
            session,
            event_type="staff.session_revoked",
            actor_id="42",
            target_id="session-7",
            result="denied",
            correlation_id="audit-boundary",
            context={"reason": "authority_changed"},
        )
        assert len(session.new) == 1
        event = next(iter(session.new))
        assert isinstance(event, SecurityAuditEvent)
        assert event.target_resource_type == "staff_session"
        assert event.context == {"reason": "authority_changed"}
    with (
        factory() as session,
        session.begin(),
        pytest.raises(ValueError, match="unapproved field"),
    ):
        writer.append(
            session,
            event_type="staff.login",
            actor_id="42",
            result="success",
            correlation_id="audit-boundary",
            context={"password": "must-not-be-recorded"},
        )
    with factory() as session:
        event = session.scalar(select(SecurityAuditEvent))
        assert event is not None and event.correlation_id == "audit-boundary"


def test_invalid_login_uses_generic_denial_and_writes_no_email_to_audit(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    for email, password in [
        ("missing@example.test", "wrong-password"),
        ("agent@example.test", "wrong-password"),
    ]:
        with pytest.raises(StaffLoginInvalidError):
            service.login(
                StaffLoginCommand(email=email, password=password),
                correlation_id="failed-login",
            )
    with factory() as session:
        events = list(session.scalars(select(SecurityAuditEvent)))
        assert len(events) == 2
        assert all(event.event_type == "staff.login_denied" for event in events)
        assert all(event.actor_id is None for event in events)
        assert all(event.context == {"reason": "invalid_credentials"} for event in events)


def test_login_denies_admin_and_requires_authoritative_activation_record(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, user_ids = service_setup
    with factory() as session, session.begin():
        admin = session.get(AppUser, user_ids["admin"])
        assert admin is not None
        session.add(
            StaffInvitation(
                id=2,
                email=admin.email,
                role_id=2,
                token_hash="b" * 64,
                created_by_admin_user_id=admin.id,
                created_at=FrozenClock().now(),
                expires_at=datetime(2027, 1, 1, tzinfo=UTC),
                consumed_at=FrozenClock().now(),
                activated_user_id=admin.id,
            )
        )
    with pytest.raises(StaffLoginInvalidError):
        service.login(
            StaffLoginCommand(email="admin@example.test", password="correct-password"),
            correlation_id="test-correlation-2",
        )
    with factory() as session, session.begin():
        session.query(StaffInvitation).filter(StaffInvitation.activated_user_id == 2).delete()
    with pytest.raises(StaffLoginInvalidError):
        service.login(
            StaffLoginCommand(email="agent@example.test", password="correct-password"),
            correlation_id="test-correlation-3",
        )
    with factory() as session:
        assert session.scalar(select(UserSession)) is None
        denied = list(session.scalars(select(SecurityAuditEvent)))
        assert len(denied) == 2
        assert all(event.result == "denied" for event in denied)


def test_login_reports_conflict_without_replacing_live_staff_session(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    first = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="first-login",
    )
    with pytest.raises(StaffSessionExistsError):
        service.login(
            StaffLoginCommand(email="agent@example.test", password="correct-password"),
            correlation_id="second-login",
        )
    with factory() as session:
        sessions = list(session.scalars(select(UserSession)))
        assert len(sessions) == 1
        assert sessions[0].revoked_at is None
        assert sessions[0].access_token_hash != first[1]


def test_refresh_rotates_once_and_replay_revokes_the_browser_session(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )
    replacement = service.refresh(initial[2], correlation_id="refresh")
    assert replacement[2] != initial[2]
    with pytest.raises(StaffSessionInvalidError):
        service.refresh(initial[2], correlation_id="replay")
    with factory() as session:
        browser_session = session.scalar(select(UserSession))
        assert browser_session is not None
        assert browser_session.revoked_at is not None
        assert browser_session.revocation_reason == "replay"
        events = list(session.scalars(select(SecurityAuditEvent)))
        assert {event.event_type for event in events} >= {
            "staff.login",
            "staff.refresh",
            "staff.session_revoked",
        }


def test_refresh_replay_audit_failure_keeps_browser_session_revoked(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, _ = service_setup
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )
    service.refresh(initial[2], correlation_id="refresh")

    def fail_audit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(SecurityAuditWriter, "append", staticmethod(fail_audit))
    with pytest.raises(AuditUnavailableError):
        service.refresh(initial[2], correlation_id="replay-audit-failure")

    with factory() as session:
        browser_session = session.scalar(select(UserSession))
        assert browser_session is not None
        assert browser_session.revoked_at is not None
        assert browser_session.revocation_reason == "replay"


def test_logout_uses_access_cookie_when_refresh_cookie_is_unusable(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )
    service.logout("invalid-refresh", initial[1], correlation_id="logout")
    with factory() as session:
        browser_session = session.scalar(select(UserSession))
        event = session.scalar(
            select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == "staff.logout")
        )
        assert browser_session is not None and browser_session.revocation_reason == "logout"
        assert event is not None and event.correlation_id == "logout"


def test_logout_falls_back_to_live_access_session_when_refresh_session_is_revoked(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    revoked = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="first-login",
    )
    with factory() as session, session.begin():
        old_session = session.scalar(select(UserSession))
        assert old_session is not None
        old_session.revoked_at = FrozenClock().now()
        old_session.revocation_reason = "replay"

    live = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="second-login",
    )
    service.logout(revoked[2], live[1], correlation_id="mismatched-cookies")

    with factory() as session:
        old_session = session.scalar(
            select(UserSession).where(
                UserSession.access_token_hash == CredentialCodec().hash_presented(revoked[1])
            )
        )
        live_session = session.scalar(
            select(UserSession).where(
                UserSession.access_token_hash == CredentialCodec().hash_presented(live[1])
            )
        )
        event = session.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.event_type == "staff.logout",
                SecurityAuditEvent.correlation_id == "mismatched-cookies",
            )
        )
        assert old_session is not None and old_session.revocation_reason == "replay"
        assert live_session is not None and live_session.revocation_reason == "logout"
        assert event is not None and event.target_resource_id == str(live_session.id)


def test_browser_credentials_do_not_authenticate_mobile_access_or_refresh(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    from finance_ai.db.repositories.sessions import SQLAlchemySessionRepository

    _, factory, _ = service_setup
    _, access, refresh, _, _, _ = service_setup[0].login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="mobile-isolation",
    )
    with factory() as session:
        repository = SQLAlchemySessionRepository(session)
        principal = repository.find_principal_by_access_hash(
            access_token_hash=CredentialCodec().hash_presented(access),
            now=FrozenClock().now(),
        )
        assert principal is None
        assert (
            repository.resolve_session_id_by_renewable_hash(
                CredentialCodec().hash_presented(refresh)
            )
            is not None
        )


def test_current_role_change_revokes_browser_session_and_audits_denial(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )
    with factory() as session, session.begin():
        agent = session.get(AppUser, 2)
        assert agent is not None
        agent.role_id = 1
    with pytest.raises(StaffAccessDeniedError):
        service.resolve(initial[1], correlation_id="role-change")
    with factory() as session:
        browser_session = session.scalar(select(UserSession))
        event = session.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.event_type == "staff.session_revoked"
            )
        )
        assert browser_session is not None
        assert browser_session.revocation_reason == "authority_changed"
        assert event is not None and event.correlation_id == "role-change"


def test_session_projection_rejects_missing_revoked_and_expired_credentials(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    with pytest.raises(StaffAuthenticationRequiredError):
        service.resolve("malformed")
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )
    with factory() as session, session.begin():
        browser_session = session.scalar(select(UserSession))
        assert browser_session is not None
        browser_session.revoked_at = FrozenClock().now()
        browser_session.revocation_reason = "logout"
    with pytest.raises(StaffSessionInvalidError):
        service.resolve(initial[1], correlation_id="revoked")

    with factory() as session, session.begin():
        browser_session = session.scalar(select(UserSession))
        assert browser_session is not None
        browser_session.revoked_at = None
        browser_session.revocation_reason = None
        browser_session.access_expires_at = FrozenClock().now()
    with pytest.raises(StaffSessionExpiredError):
        service.resolve(initial[1], correlation_id="expired-access")


def test_refresh_rejects_missing_unknown_mobile_and_expired_credentials(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    with pytest.raises(StaffSessionInvalidError):
        service.refresh(None, correlation_id="missing")
    with pytest.raises(StaffSessionInvalidError):
        service.refresh("unknown", correlation_id="unknown")
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )
    with factory() as session, session.begin():
        browser_session = session.scalar(select(UserSession))
        assert browser_session is not None
        browser_session.platform = "android"
    with pytest.raises(StaffSessionInvalidError):
        service.refresh(initial[2], correlation_id="mobile-platform")

    with factory() as session, session.begin():
        browser_session = session.scalar(select(UserSession))
        assert browser_session is not None
        browser_session.platform = "staff_browser"
        credential = session.scalar(select(RenewableCredential))
        assert credential is not None
        credential.issued_at = datetime(2026, 11, 11, 11, 59, 59, tzinfo=UTC)
        credential.expires_at = FrozenClock().now()
    with pytest.raises(StaffAccessDeniedError):
        service.refresh(initial[2], correlation_id="expired-session")


def test_refresh_revokes_when_authority_changes_and_logout_is_idempotent(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )
    with factory() as session, session.begin():
        agent = session.get(AppUser, 2)
        assert agent is not None
        agent.is_active = False
    with pytest.raises(StaffAccessDeniedError):
        service.refresh(initial[2], correlation_id="deactivated")
    service.logout(None, None, correlation_id="empty-logout")
    service.logout(initial[2], initial[1], correlation_id="already-revoked")
    with factory() as session:
        events = list(session.scalars(select(SecurityAuditEvent)))
        assert any(event.context == {"reason": "authority_changed"} for event in events)
        assert not any(event.correlation_id == "empty-logout" for event in events)


def test_refresh_rejects_changed_role_and_records_authority_revocation(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
) -> None:
    service, factory, _ = service_setup
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )
    with factory() as session, session.begin():
        agent = session.get(AppUser, 2)
        assert agent is not None
        agent.role_id = 1
    with pytest.raises(StaffAccessDeniedError):
        service.refresh(initial[2], correlation_id="role-changed-refresh")
    with factory() as session:
        browser_session = session.scalar(select(UserSession))
        event = session.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.correlation_id == "role-changed-refresh"
            )
        )
        assert browser_session is not None
        assert browser_session.revocation_reason == "authority_changed"
        assert event is not None and event.context == {"reason": "authority_changed"}


def test_failed_login_audit_unavailability_does_not_leak_account_existence(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = service_setup

    def fail_audit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(SecurityAuditWriter, "append", staticmethod(fail_audit))
    with pytest.raises(AuditUnavailableError):
        service.login(
            StaffLoginCommand(email="missing@example.test", password="wrong-password"),
            correlation_id="denied-audit-failure",
        )


def test_refresh_audit_failure_rolls_back_single_use_rotation(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, _ = service_setup
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )
    with factory() as session:
        old_access_hash = session.scalar(select(UserSession.access_token_hash))

    def fail_audit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(SecurityAuditWriter, "append", staticmethod(fail_audit))
    with pytest.raises(AuditUnavailableError):
        service.refresh(initial[2], correlation_id="audit-failure")
    with factory() as session:
        browser_session = session.scalar(select(UserSession))
        credential = session.scalar(
            select(RenewableCredential).where(
                RenewableCredential.token_hash == CredentialCodec().hash_presented(initial[2])
            )
        )
        assert browser_session is not None and browser_session.access_token_hash == old_access_hash
        assert credential is not None and credential.consumed_at is None


def test_logout_audit_failure_does_not_claim_or_persist_revocation(
    service_setup: tuple[StaffSessionService, sessionmaker[Session], dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, _ = service_setup
    initial = service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="login",
    )

    def fail_audit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(SecurityAuditWriter, "append", staticmethod(fail_audit))
    with pytest.raises(AuditUnavailableError):
        service.logout(initial[2], initial[1], correlation_id="logout-audit-failure")
    with factory() as session:
        browser_session = session.scalar(select(UserSession))
        assert browser_session is not None and browser_session.revoked_at is None

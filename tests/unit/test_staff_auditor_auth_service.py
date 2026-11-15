"""SEC-01 consumer: Security Auditor password+TOTP sign-in and current-authority checks."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.core.errors import (
    AuditUnavailableError,
    MfaAlreadyEnrolledError,
    MfaChallengeInvalidError,
    MfaCodeInvalidError,
    MfaEnrollmentRequiredError,
    MfaUnavailableError,
    StaffAccessDeniedError,
    StaffLoginInvalidError,
    StaffSessionExistsError,
    StaffSessionInvalidError,
)
from finance_ai.db.base import Base
from finance_ai.db.models.admin import AdminTotpFactor, StaffInvitation, StaffMfaChallenge
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import RenewableCredential, UserSession
from finance_ai.staff_auth.mfa import StaffMfaPolicy, TotpSecretCipher, TotpVerifier
from finance_ai.staff_auth.schemas import StaffLoginCommand
from finance_ai.staff_auth.service import StaffMfaChallengeIssued, StaffSessionService

START = datetime(2026, 11, 14, 15, 0, 5, tzinfo=UTC)
AUDITOR_ID = 3
SUPPORT_ID = 2


class MovableClock:
    def __init__(self) -> None:
        self.value = START

    def now(self) -> datetime:
        return self.value


class FakePasswordHasher:
    def verify_real_or_dummy(self, password_hash: str | None, candidate: str) -> bool:
        return password_hash == "password-hash" and candidate == "correct-password"


@dataclass
class Setup:
    service: StaffSessionService
    factory: sessionmaker[Session]
    clock: MovableClock
    policy: StaffMfaPolicy


def build_policy(max_attempts: int = 3) -> StaffMfaPolicy:
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    return StaffMfaPolicy(
        cipher=TotpSecretCipher.from_encoded(key),
        verifier=TotpVerifier(),
        issuer="FinanciAI",
        challenge_minutes=5,
        max_attempts=max_attempts,
    )


@pytest.fixture
def setup() -> Setup:
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
    Base.metadata.create_all(
        engine,
        tables=[
            Role.__table__,
            Currency.__table__,
            AppUser.__table__,
            UserSession.__table__,
            RenewableCredential.__table__,
            StaffInvitation.__table__,
            AdminTotpFactor.__table__,
            StaffMfaChallenge.__table__,
            SecurityAuditEvent.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as session, session.begin():
        session.add_all(
            [
                Role(id=1, name="user"),
                Role(id=2, name="admin"),
                Role(id=3, name="support_agent"),
                Role(id=4, name="security_auditor"),
                Currency(id=1, code="COP", name="Colombian peso", decimals=2, is_active=True),
            ]
        )
        session.flush()
        for user_id, email, role_id in (
            (1, "admin@example.test", 2),
            (SUPPORT_ID, "agent@example.test", 3),
            (AUDITOR_ID, "auditor@example.test", 4),
        ):
            session.add(
                AppUser(
                    id=user_id,
                    email=email,
                    password_hash="password-hash",
                    role_id=role_id,
                    preferred_currency_id=1,
                    is_active=True,
                )
            )
        session.flush()
        for invitation_id, user_id, email, role_id in (
            (1, SUPPORT_ID, "agent@example.test", 3),
            (2, AUDITOR_ID, "auditor@example.test", 4),
        ):
            session.add(
                StaffInvitation(
                    id=invitation_id,
                    email=email,
                    role_id=role_id,
                    token_hash=str(invitation_id) * 64,
                    created_by_admin_user_id=1,
                    created_at=START - timedelta(days=1),
                    expires_at=START + timedelta(days=30),
                    consumed_at=START - timedelta(days=1),
                    activated_user_id=user_id,
                )
            )
    clock = MovableClock()
    policy = build_policy()
    service = StaffSessionService(
        session_factory=factory,
        password_hasher=FakePasswordHasher(),  # type: ignore[arg-type]
        credential_codec=CredentialCodec(),
        clock=clock,
        audit_writer=SecurityAuditWriter(),
        mfa=policy,
    )
    return Setup(service=service, factory=factory, clock=clock, policy=policy)


def auditor_login(setup: Setup) -> StaffMfaChallengeIssued:
    outcome = setup.service.login(
        StaffLoginCommand(email="auditor@example.test", password="correct-password"),
        correlation_id="auditor-login",
    )
    assert isinstance(outcome, StaffMfaChallengeIssued)
    return outcome


def code_at(secret: str, moment: datetime) -> str:
    return pyotp.TOTP(secret).at(moment)


def enroll_and_verify(setup: Setup) -> tuple[str, tuple[object, ...]]:
    challenge = auditor_login(setup)
    enrollment = setup.service.start_mfa_enrollment(
        challenge.raw_challenge, correlation_id="enroll"
    )
    issued = setup.service.verify_mfa(
        challenge.raw_challenge,
        code_at(enrollment.secret, setup.clock.now()),
        correlation_id="verify",
    )
    return enrollment.secret, issued


def event_types(factory: sessionmaker[Session]) -> list[str]:
    with factory() as session:
        return list(session.scalars(select(SecurityAuditEvent.event_type)))


def test_auditor_password_alone_issues_only_a_challenge_without_session(setup: Setup) -> None:
    challenge = auditor_login(setup)

    assert challenge.enrollment_required is True
    assert challenge.expires_at == START + timedelta(minutes=5)
    with setup.factory() as session:
        assert session.scalar(select(UserSession)) is None
        stored = session.scalar(select(StaffMfaChallenge))
        assert stored is not None and stored.token_hash != challenge.raw_challenge
    assert event_types(setup.factory) == []


def test_enrollment_then_valid_code_issues_mfa_verified_session_and_events(setup: Setup) -> None:
    challenge = auditor_login(setup)
    enrollment = setup.service.start_mfa_enrollment(
        challenge.raw_challenge, correlation_id="enroll"
    )
    assert enrollment.account_name == "auditor@example.test"
    assert enrollment.otpauth_uri.startswith("otpauth://totp/FinanciAI:")
    with setup.factory() as session:
        factor = session.scalar(select(AdminTotpFactor))
        assert factor is not None and factor.state == "pending"
        assert enrollment.secret.encode() not in factor.secret_ciphertext

    user, access, refresh, *_ = setup.service.verify_mfa(
        challenge.raw_challenge, code_at(enrollment.secret, START), correlation_id="verify"
    )

    assert user.role == "security_auditor"
    assert access and refresh and access != refresh
    with setup.factory() as session:
        browser_session = session.scalar(select(UserSession))
        factor = session.scalar(select(AdminTotpFactor))
        stored_challenge = session.scalar(select(StaffMfaChallenge))
        events = list(session.scalars(select(SecurityAuditEvent)))
    assert browser_session is not None and browser_session.mfa_verified_at is not None
    assert factor is not None and factor.state == "active"
    assert factor.last_accepted_time_step == int(START.timestamp()) // 30
    assert stored_challenge is not None and stored_challenge.consumed_at is not None
    assert [item.event_type for item in events] == [
        "auth.mfa_enrolled",
        "auth.mfa_success",
        "auth.login_success",
    ]
    login_event = events[-1]
    assert login_event.actor_id == str(AUDITOR_ID)
    assert login_event.target_resource_type == "staff_session"
    assert all(enrollment.secret not in str(item.context) for item in events)
    assert setup.service.resolve(access).role == "security_auditor"


def test_reused_code_for_the_same_time_step_is_rejected_as_replay(setup: Setup) -> None:
    secret, issued = enroll_and_verify(setup)
    setup.service.logout(str(issued[2]), str(issued[1]), correlation_id="logout")
    challenge = auditor_login(setup)
    assert challenge.enrollment_required is False

    with pytest.raises(MfaCodeInvalidError):
        setup.service.verify_mfa(
            challenge.raw_challenge, code_at(secret, START), correlation_id="replay"
        )
    with setup.factory() as session:
        failed = session.scalar(
            select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == "auth.mfa_failed")
        )
    assert failed is not None and failed.context == {"reason": "replayed_code"}

    setup.clock.value = START + timedelta(seconds=30)
    user, *_ = setup.service.verify_mfa(
        challenge.raw_challenge, code_at(secret, setup.clock.value), correlation_id="next"
    )
    assert user.id == AUDITOR_ID


def test_failed_codes_are_bounded_per_challenge(setup: Setup) -> None:
    challenge = auditor_login(setup)
    enrollment = setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="e")
    wrong = "000000" if code_at(enrollment.secret, START) != "000000" else "111111"
    for _ in range(setup.policy.max_attempts - 1):
        with pytest.raises(MfaCodeInvalidError):
            setup.service.verify_mfa(challenge.raw_challenge, wrong, correlation_id="bad")
    with pytest.raises(MfaChallengeInvalidError):
        setup.service.verify_mfa(challenge.raw_challenge, wrong, correlation_id="bad")
    with pytest.raises(MfaChallengeInvalidError):
        setup.service.verify_mfa(
            challenge.raw_challenge, code_at(enrollment.secret, START), correlation_id="late"
        )
    assert event_types(setup.factory).count("auth.mfa_failed") == setup.policy.max_attempts
    with setup.factory() as session:
        assert session.scalar(select(UserSession)) is None


def test_expired_unknown_and_cancelled_challenges_confer_nothing(setup: Setup) -> None:
    with pytest.raises(MfaChallengeInvalidError):
        setup.service.verify_mfa("not-a-credential", "123456", correlation_id="x")
    with pytest.raises(MfaChallengeInvalidError):
        setup.service.verify_mfa(CredentialCodec().issue().raw, "123456", correlation_id="x")

    challenge = auditor_login(setup)
    setup.clock.value = START + timedelta(minutes=5)
    with pytest.raises(MfaChallengeInvalidError):
        setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="late")

    setup.clock.value = START
    fresh = auditor_login(setup)
    setup.service.cancel_mfa(fresh.raw_challenge)
    setup.service.cancel_mfa(None)
    with pytest.raises(MfaChallengeInvalidError):
        setup.service.start_mfa_enrollment(fresh.raw_challenge, correlation_id="cancelled")


def test_new_login_invalidates_previous_open_challenge(setup: Setup) -> None:
    first = auditor_login(setup)
    auditor_login(setup)
    with pytest.raises(MfaChallengeInvalidError):
        setup.service.start_mfa_enrollment(first.raw_challenge, correlation_id="old")


def test_code_without_enrollment_and_second_enrollment_are_rejected(setup: Setup) -> None:
    challenge = auditor_login(setup)
    with pytest.raises(MfaEnrollmentRequiredError):
        setup.service.verify_mfa(challenge.raw_challenge, "123456", correlation_id="x")
    secret, issued = enroll_and_verify(setup)
    del secret
    setup.service.logout(str(issued[2]), str(issued[1]), correlation_id="logout")
    again = auditor_login(setup)
    with pytest.raises(MfaAlreadyEnrolledError):
        setup.service.start_mfa_enrollment(again.raw_challenge, correlation_id="again")


def test_restarting_enrollment_disables_previous_pending_factor(setup: Setup) -> None:
    challenge = auditor_login(setup)
    first = setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="1")
    second = setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="2")
    assert first.secret != second.secret
    with setup.factory() as session:
        states = sorted(session.scalars(select(AdminTotpFactor.state)))
    assert states == ["disabled", "pending"]
    with pytest.raises(MfaCodeInvalidError):
        setup.service.verify_mfa(
            challenge.raw_challenge, code_at(first.secret, START), correlation_id="old"
        )


def test_mfa_is_required_and_fails_closed_without_configuration(setup: Setup) -> None:
    unconfigured = StaffSessionService(
        session_factory=setup.factory,
        password_hasher=FakePasswordHasher(),  # type: ignore[arg-type]
        credential_codec=CredentialCodec(),
        clock=setup.clock,
        audit_writer=SecurityAuditWriter(),
    )
    with pytest.raises(MfaUnavailableError):
        unconfigured.login(
            StaffLoginCommand(email="auditor@example.test", password="correct-password"),
            correlation_id="no-mfa",
        )
    with pytest.raises(MfaUnavailableError):
        unconfigured.verify_mfa("x", "123456", correlation_id="no-mfa")
    with setup.factory() as session:
        denied = session.scalar(select(SecurityAuditEvent))
        assert session.scalar(select(UserSession)) is None
    assert denied is not None and denied.context == {"reason": "mfa_unavailable"}


def test_unreadable_factor_secret_fails_closed(setup: Setup) -> None:
    challenge = auditor_login(setup)
    setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="e")
    other_key = base64.urlsafe_b64encode(bytes(range(1, 33))).decode("ascii")
    rotated = StaffSessionService(
        session_factory=setup.factory,
        password_hasher=FakePasswordHasher(),  # type: ignore[arg-type]
        credential_codec=CredentialCodec(),
        clock=setup.clock,
        audit_writer=SecurityAuditWriter(),
        mfa=StaffMfaPolicy(
            cipher=TotpSecretCipher.from_encoded(other_key),
            verifier=TotpVerifier(),
            issuer="FinanciAI",
            challenge_minutes=5,
            max_attempts=3,
        ),
    )
    with pytest.raises(MfaUnavailableError):
        rotated.verify_mfa(challenge.raw_challenge, "123456", correlation_id="rotated")


def test_suspension_or_role_loss_during_challenge_denies_verification(setup: Setup) -> None:
    challenge = auditor_login(setup)
    enrollment = setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="e")
    with setup.factory() as session, session.begin():
        session.execute(update(AppUser).where(AppUser.id == AUDITOR_ID).values(is_active=False))
    with pytest.raises(StaffAccessDeniedError):
        setup.service.verify_mfa(
            challenge.raw_challenge, code_at(enrollment.secret, START), correlation_id="v"
        )
    with setup.factory() as session:
        assert session.scalar(select(UserSession)) is None


def test_existing_staff_session_blocks_a_second_auditor_session(setup: Setup) -> None:
    secret, _issued = enroll_and_verify(setup)
    with pytest.raises(StaffSessionExistsError):
        auditor_login(setup)
    del secret


def test_audit_failure_during_verification_issues_no_session(setup: Setup) -> None:
    challenge = auditor_login(setup)
    enrollment = setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="e")

    class FailingWriter(SecurityAuditWriter):
        def append(self, session: Session, **kwargs: object) -> None:  # type: ignore[override]
            raise RuntimeError("audit store unavailable")

    failing = StaffSessionService(
        session_factory=setup.factory,
        password_hasher=FakePasswordHasher(),  # type: ignore[arg-type]
        credential_codec=CredentialCodec(),
        clock=setup.clock,
        audit_writer=FailingWriter(),
        mfa=setup.policy,
    )
    with pytest.raises(AuditUnavailableError):
        failing.verify_mfa(
            challenge.raw_challenge, code_at(enrollment.secret, START), correlation_id="v"
        )
    with setup.factory() as session:
        assert session.scalar(select(UserSession)) is None
        factor = session.scalar(select(AdminTotpFactor))
        assert factor is not None and factor.state == "pending"


def test_existing_auditor_session_is_denied_immediately_after_suspension(setup: Setup) -> None:
    _secret, issued = enroll_and_verify(setup)
    access = str(issued[1])
    with setup.factory() as session, session.begin():
        session.execute(update(AppUser).where(AppUser.id == AUDITOR_ID).values(is_active=False))
    with pytest.raises(StaffAccessDeniedError):
        setup.service.resolve(access)
    with pytest.raises(StaffSessionInvalidError):
        setup.service.resolve(access)
    with setup.factory() as session:
        revoked = session.scalar(select(UserSession))
    assert revoked is not None and revoked.revocation_reason == "authority_changed"


def test_existing_auditor_session_is_denied_after_role_removal(setup: Setup) -> None:
    _secret, issued = enroll_and_verify(setup)
    with setup.factory() as session, session.begin():
        session.execute(update(AppUser).where(AppUser.id == AUDITOR_ID).values(role_id=2))
    with pytest.raises(StaffAccessDeniedError):
        setup.service.resolve(str(issued[1]))
    # The session is now revoked; renewal stays denied.
    with pytest.raises(StaffAccessDeniedError):
        setup.service.refresh(str(issued[2]), correlation_id="refresh")


def test_promoted_session_without_mfa_never_gains_auditor_authority(setup: Setup) -> None:
    outcome = setup.service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="support",
    )
    assert not isinstance(outcome, StaffMfaChallengeIssued)
    _user, access, refresh, *_ = outcome
    with setup.factory() as session, session.begin():
        session.execute(update(AppUser).where(AppUser.id == SUPPORT_ID).values(role_id=4))
        session.execute(update(StaffInvitation).where(StaffInvitation.id == 1).values(role_id=4))
    with pytest.raises(StaffAccessDeniedError):
        setup.service.refresh(refresh, correlation_id="promoted")
    with pytest.raises(StaffSessionInvalidError):
        setup.service.resolve(access)
    with setup.factory() as session:
        revoked = session.scalar(select(UserSession))
    assert revoked is not None and revoked.revocation_reason == "authority_changed"


def test_auditor_refresh_keeps_mfa_assurance(setup: Setup) -> None:
    _secret, issued = enroll_and_verify(setup)
    setup.clock.value = START + timedelta(minutes=10)
    user, access, *_ = setup.service.refresh(str(issued[2]), correlation_id="refresh")
    assert user.role == "security_auditor"
    assert setup.service.resolve(access).id == AUDITOR_ID


def test_support_and_admin_behaviour_is_unchanged(setup: Setup) -> None:
    outcome = setup.service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="support",
    )
    assert not isinstance(outcome, StaffMfaChallengeIssued)
    assert outcome[0].role == "support_agent"
    with pytest.raises(StaffLoginInvalidError):
        setup.service.login(
            StaffLoginCommand(email="admin@example.test", password="correct-password"),
            correlation_id="admin",
        )
    assert event_types(setup.factory) == ["staff.login", "staff.login_denied"]

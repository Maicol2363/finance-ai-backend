"""SEC-01 recovery codes and authenticator replacement for privileged staff."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.core.errors import (
    MfaRecoveryInvalidError,
    MfaUnavailableError,
    PasswordConfirmationInvalidError,
    StaffSessionInvalidError,
)
from finance_ai.db.base import Base
from finance_ai.db.models.admin import AdminTotpFactor, StaffMfaChallenge, StaffMfaRecoveryCode
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.sessions import UserSession
from finance_ai.staff_auth.recovery import (
    RECOVERY_CODE_COUNT,
    StaffRecoveryService,
    hash_recovery_code,
    issued_code_hashes,
    new_recovery_codes,
)
from finance_ai.staff_auth.schemas import StaffLoginCommand
from finance_ai.staff_auth.service import StaffMfaChallengeIssued
from tests.unit import test_staff_auditor_auth_service as auditor_tests
from tests.unit.test_staff_auditor_auth_service import (
    AUDITOR_ID,
    START,
    FakePasswordHasher,
    Setup,
    code_at,
)

ADMIN_ID = 1
setup = auditor_tests.setup


@pytest.fixture
def recovery(setup: Setup) -> Iterator[tuple[Setup, StaffRecoveryService]]:
    bind = setup.factory.kw["bind"]
    Base.metadata.create_all(bind, tables=[StaffMfaRecoveryCode.__table__])  # type: ignore[list-item]
    yield (
        setup,
        StaffRecoveryService(
            session_factory=setup.factory,
            password_hasher=FakePasswordHasher(),  # type: ignore[arg-type]
            credential_codec=CredentialCodec(),
            clock=setup.clock,
            audit_writer=SecurityAuditWriter(),
            mfa=setup.policy,
        ),
    )


def sign_in(setup: Setup, email: str) -> tuple[str, tuple[object, ...]]:
    challenge = setup.service.login(
        StaffLoginCommand(email=email, password="correct-password"), correlation_id="login"
    )
    assert isinstance(challenge, StaffMfaChallengeIssued)
    enrollment = setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="e")
    issued = setup.service.verify_mfa(
        challenge.raw_challenge, code_at(enrollment.secret, setup.clock.now()), correlation_id="v"
    )
    return enrollment.secret, issued


def events(setup: Setup) -> list[tuple[str, str, str | None]]:
    with setup.factory() as session:
        rows = session.execute(
            select(
                SecurityAuditEvent.event_type, SecurityAuditEvent.result, SecurityAuditEvent.context
            ).order_by(SecurityAuditEvent.occurred_at)
        )
        return [(row[0], row[1], str(row[2])) for row in rows]


def test_recovery_codes_have_the_documented_shape_and_hash_normalization() -> None:
    codes = new_recovery_codes()

    assert len(codes) == RECOVERY_CODE_COUNT == len(set(codes))
    assert all(len(code) == 19 and code.count("-") == 3 for code in codes)
    first = codes[0]
    assert hash_recovery_code(first) == hash_recovery_code(first.lower().replace("-", " "))
    assert hash_recovery_code("too-short") is None
    assert hash_recovery_code("1111-1111-1111-1111") is None


def test_issued_codes_always_hash_and_a_malformed_one_is_a_generator_defect() -> None:
    codes = new_recovery_codes()
    assert issued_code_hashes(codes) == [hash_recovery_code(code) for code in codes]
    # Explicit validation, not an assert: it holds with -O and is not swallowed by callers.
    with pytest.raises(RuntimeError):
        issued_code_hashes([*codes[:-1], "too-short"])


def test_issue_codes_stores_only_hashes_and_replaces_previous_set(
    recovery: tuple[Setup, StaffRecoveryService],
) -> None:
    setup, service = recovery
    sign_in(setup, "admin@example.test")

    first = service.issue_recovery_codes(ADMIN_ID, "correct-password", correlation_id="c1")
    second = service.issue_recovery_codes(ADMIN_ID, "correct-password", correlation_id="c2")

    assert len(first) == len(second) == RECOVERY_CODE_COUNT
    with setup.factory() as session:
        rows = list(session.scalars(select(StaffMfaRecoveryCode)))
    assert len(rows) == 2 * RECOVERY_CODE_COUNT
    open_hashes = {row.code_hash for row in rows if row.consumed_at is None}
    assert open_hashes == {hash_recovery_code(code) for code in second}
    assert not any(code.replace("-", "") in row.code_hash for row in rows for code in first)


def test_issue_codes_requires_the_current_password(
    recovery: tuple[Setup, StaffRecoveryService],
) -> None:
    setup, service = recovery
    sign_in(setup, "admin@example.test")

    with pytest.raises(PasswordConfirmationInvalidError):
        service.issue_recovery_codes(ADMIN_ID, "wrong-password", correlation_id="c")

    with setup.factory() as session:
        assert session.scalar(select(StaffMfaRecoveryCode)) is None


def test_recovery_code_resets_authenticator_revokes_sessions_and_is_single_use(
    recovery: tuple[Setup, StaffRecoveryService],
) -> None:
    setup, service = recovery
    _secret, issued = sign_in(setup, "admin@example.test")
    codes = service.issue_recovery_codes(ADMIN_ID, "correct-password", correlation_id="c")

    challenge = service.reset_authenticator(
        email="Admin@Example.test",
        password="correct-password",
        recovery_code=codes[0].lower(),
        totp_code=None,
        correlation_id="reset",
    )

    assert challenge.enrollment_required is True
    with setup.factory() as session:
        factor = session.scalar(select(AdminTotpFactor))
        assert factor is not None and factor.state == "disabled"
        stored = session.scalar(select(UserSession))
        assert stored is not None and stored.revocation_reason == "mfa_reset"
        assert (
            session.scalar(
                select(StaffMfaRecoveryCode).where(StaffMfaRecoveryCode.consumed_at.is_(None))
            )
            is None
        )
        open_challenges = list(
            session.scalars(
                select(StaffMfaChallenge).where(StaffMfaChallenge.consumed_at.is_(None))
            )
        )
        assert len(open_challenges) == 1
    with pytest.raises(StaffSessionInvalidError):
        setup.service.resolve(str(issued[1]))
    assert ("auth.mfa_recovered", "success", "{'reason': 'recovery_code'}") in events(setup)
    with pytest.raises(MfaRecoveryInvalidError):
        service.reset_authenticator(
            email="admin@example.test",
            password="correct-password",
            recovery_code=codes[0],
            totp_code=None,
            correlation_id="replay",
        )


def test_new_authenticator_can_be_enrolled_after_recovery(
    recovery: tuple[Setup, StaffRecoveryService],
) -> None:
    setup, service = recovery
    old_secret, _issued = sign_in(setup, "admin@example.test")
    codes = service.issue_recovery_codes(ADMIN_ID, "correct-password", correlation_id="c")
    challenge = service.reset_authenticator(
        email="admin@example.test",
        password="correct-password",
        recovery_code=codes[1],
        totp_code=None,
        correlation_id="reset",
    )

    enrollment = setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="e2")
    setup.clock.value = START + timedelta(minutes=1)
    issued = setup.service.verify_mfa(
        challenge.raw_challenge, code_at(enrollment.secret, setup.clock.now()), correlation_id="v2"
    )

    assert issued[0].role == "admin"
    assert enrollment.secret != old_secret


def test_current_authenticator_code_replaces_it_and_cannot_be_replayed(
    recovery: tuple[Setup, StaffRecoveryService],
) -> None:
    setup, service = recovery
    secret, _issued = sign_in(setup, "admin@example.test")

    # The code accepted at sign-in cannot be reused for the same time step.
    with pytest.raises(MfaRecoveryInvalidError):
        service.reset_authenticator(
            email="admin@example.test",
            password="correct-password",
            recovery_code=None,
            totp_code=code_at(secret, setup.clock.now()),
            correlation_id="replay",
        )
    setup.clock.value = START + timedelta(seconds=60)
    outcome = service.reset_authenticator(
        email="admin@example.test",
        password="correct-password",
        recovery_code=None,
        totp_code=code_at(secret, setup.clock.now()),
        correlation_id="replace",
    )

    assert outcome.enrollment_required is True
    assert any(event[0] == "auth.mfa_replaced" for event in events(setup))


@pytest.mark.parametrize(
    ("email", "password", "use_recovery", "use_totp"),
    [
        ("admin@example.test", "wrong-password", True, False),
        ("nobody@example.test", "correct-password", True, False),
        ("admin@example.test", "correct-password", False, False),
        ("admin@example.test", "correct-password", True, True),
    ],
)
def test_every_failure_is_generic_and_consumes_nothing(
    recovery: tuple[Setup, StaffRecoveryService],
    email: str,
    password: str,
    use_recovery: bool,
    use_totp: bool,
) -> None:
    setup, service = recovery
    secret, _issued = sign_in(setup, "admin@example.test")
    codes = service.issue_recovery_codes(ADMIN_ID, "correct-password", correlation_id="c")
    setup.clock.value = START + timedelta(seconds=90)

    with pytest.raises(MfaRecoveryInvalidError):
        service.reset_authenticator(
            email=email,
            password=password,
            recovery_code=codes[0] if use_recovery else None,
            totp_code=code_at(secret, setup.clock.now()) if use_totp else None,
            correlation_id="bad",
        )

    with setup.factory() as session:
        factor = session.scalar(select(AdminTotpFactor))
        assert factor is not None and factor.state == "active"
        open_codes = list(
            session.scalars(
                select(StaffMfaRecoveryCode).where(StaffMfaRecoveryCode.consumed_at.is_(None))
            )
        )
        assert len(open_codes) == RECOVERY_CODE_COUNT
    assert events(setup)[-1][:2] == ("auth.mfa_failed", "failure")


def test_suspended_account_cannot_recover_and_keeps_its_code(
    recovery: tuple[Setup, StaffRecoveryService],
) -> None:
    setup, service = recovery
    sign_in(setup, "admin@example.test")
    codes = service.issue_recovery_codes(ADMIN_ID, "correct-password", correlation_id="c")
    with setup.factory() as session, session.begin():
        session.execute(update(AppUser).where(AppUser.id == ADMIN_ID).values(is_active=False))

    with pytest.raises(MfaRecoveryInvalidError):
        service.reset_authenticator(
            email="admin@example.test",
            password="correct-password",
            recovery_code=codes[0],
            totp_code=None,
            correlation_id="suspended",
        )

    with setup.factory() as session:
        assert (
            session.scalar(
                select(StaffMfaRecoveryCode).where(StaffMfaRecoveryCode.consumed_at.is_not(None))
            )
            is None
        )


def test_roles_without_mandatory_mfa_cannot_use_reset(
    recovery: tuple[Setup, StaffRecoveryService],
) -> None:
    _setup, service = recovery

    with pytest.raises(MfaRecoveryInvalidError):
        service.reset_authenticator(
            email="agent@example.test",
            password="correct-password",
            recovery_code="AAAA-BBBB-CCCC-DDDD",
            totp_code=None,
            correlation_id="support",
        )


def test_auditor_can_recover_with_the_same_flow(
    recovery: tuple[Setup, StaffRecoveryService],
) -> None:
    setup, service = recovery
    sign_in(setup, "auditor@example.test")
    codes = service.issue_recovery_codes(AUDITOR_ID, "correct-password", correlation_id="c")

    outcome = service.reset_authenticator(
        email="auditor@example.test",
        password="correct-password",
        recovery_code=codes[2],
        totp_code=None,
        correlation_id="reset",
    )

    assert outcome.enrollment_required is True


def test_reset_fails_closed_without_a_configured_authenticator_policy(setup: Setup) -> None:
    service = StaffRecoveryService(
        session_factory=setup.factory,
        password_hasher=FakePasswordHasher(),  # type: ignore[arg-type]
        credential_codec=CredentialCodec(),
        clock=setup.clock,
        audit_writer=SecurityAuditWriter(),
        mfa=None,
    )

    with pytest.raises(MfaUnavailableError):
        service.reset_authenticator(
            email="admin@example.test",
            password="correct-password",
            recovery_code="AAAA-BBBB-CCCC-DDDD",
            totp_code=None,
            correlation_id="x",
        )

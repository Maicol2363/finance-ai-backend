"""SEC-01 for the Administrator: password+TOTP, authority rules, lifetimes, role binding."""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from sqlalchemy import select, update

from finance_ai.core.errors import StaffAccessDeniedError, StaffSessionInvalidError
from finance_ai.db.models.admin import AdminTotpFactor
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.sessions import UserSession
from finance_ai.staff_auth.schemas import StaffLoginCommand
from finance_ai.staff_auth.service import StaffMfaChallengeIssued
from tests.unit import test_staff_auditor_auth_service as auditor_tests
from tests.unit.test_staff_auditor_auth_service import (
    AUDITOR_ID,
    START,
    SUPPORT_ID,
    Setup,
    code_at,
    event_types,
)

# Reuse the Security Auditor fixture (same roles, users and MFA policy).
setup = auditor_tests.setup

ADMIN_ID = 1


def admin_challenge(setup: Setup) -> StaffMfaChallengeIssued:
    outcome = setup.service.login(
        StaffLoginCommand(email="admin@example.test", password="correct-password"),
        correlation_id="admin-login",
    )
    assert isinstance(outcome, StaffMfaChallengeIssued)
    return outcome


def admin_enroll_and_verify(setup: Setup) -> tuple[str, tuple[object, ...]]:
    challenge = admin_challenge(setup)
    enrollment = setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="e")
    issued = setup.service.verify_mfa(
        challenge.raw_challenge, code_at(enrollment.secret, setup.clock.now()), correlation_id="v"
    )
    return enrollment.secret, issued


def test_admin_password_alone_never_issues_a_session(setup: Setup) -> None:
    challenge = admin_challenge(setup)

    assert challenge.enrollment_required is True
    with setup.factory() as session:
        assert session.scalar(select(UserSession)) is None


def test_admin_session_uses_short_admin_lifetimes_and_records_events(setup: Setup) -> None:
    _secret, issued = admin_enroll_and_verify(setup)

    user, _access, _refresh, access_expires, idle_expires, absolute_expires = issued  # type: ignore[misc]
    assert user.role == "admin"
    assert access_expires == START + timedelta(minutes=10)
    assert idle_expires == START + timedelta(minutes=15)
    assert absolute_expires == START + timedelta(hours=4)
    with setup.factory() as session:
        stored = session.scalar(select(UserSession))
        assert stored is not None
        assert stored.mfa_verified_at is not None
        assert stored.mfa_verified_at.replace(tzinfo=UTC) == START
        assert stored.issued_role_id == 2
    assert sorted(event_types(setup.factory)) == [
        "auth.login_success",
        "auth.mfa_enrolled",
        "auth.mfa_success",
    ]


def test_admin_refresh_keeps_admin_lifetimes_inside_the_absolute_limit(setup: Setup) -> None:
    _secret, issued = admin_enroll_and_verify(setup)
    setup.clock.value = START + timedelta(minutes=9)

    _user, _access, _refresh, access_expires, idle_expires, absolute_expires = (
        setup.service.refresh(str(issued[2]), correlation_id="refresh")
    )

    assert access_expires == START + timedelta(minutes=19)
    assert idle_expires == START + timedelta(minutes=24)
    assert absolute_expires == START + timedelta(hours=4)


def test_admin_session_is_denied_when_the_authenticator_is_no_longer_active(
    setup: Setup,
) -> None:
    _secret, issued = admin_enroll_and_verify(setup)
    with setup.factory() as session, session.begin():
        session.execute(
            update(AdminTotpFactor)
            .where(AdminTotpFactor.admin_user_id == ADMIN_ID)
            .values(state="disabled", activated_at=None)
        )

    with pytest.raises(StaffAccessDeniedError):
        setup.service.resolve(str(issued[1]))
    with pytest.raises(StaffSessionInvalidError):
        setup.service.resolve(str(issued[1]))


def test_admin_session_without_mfa_assurance_is_denied(setup: Setup) -> None:
    _secret, issued = admin_enroll_and_verify(setup)
    with setup.factory() as session, session.begin():
        session.execute(update(UserSession).values(mfa_verified_at=None))

    with pytest.raises(StaffAccessDeniedError):
        setup.service.resolve(str(issued[1]))


def test_session_from_before_promotion_never_gains_admin_authority(setup: Setup) -> None:
    outcome = setup.service.login(
        StaffLoginCommand(email="agent@example.test", password="correct-password"),
        correlation_id="support",
    )
    assert not isinstance(outcome, StaffMfaChallengeIssued)
    _user, access, refresh, *_ = outcome
    with setup.factory() as session, session.begin():
        session.execute(update(AppUser).where(AppUser.id == SUPPORT_ID).values(role_id=2))

    with pytest.raises(StaffAccessDeniedError):
        setup.service.refresh(refresh, correlation_id="promoted")
    with pytest.raises(StaffSessionInvalidError):
        setup.service.resolve(access)


def test_auditor_mfa_session_does_not_carry_over_to_admin_role(setup: Setup) -> None:
    challenge = setup.service.login(
        StaffLoginCommand(email="auditor@example.test", password="correct-password"),
        correlation_id="auditor",
    )
    assert isinstance(challenge, StaffMfaChallengeIssued)
    enrollment = setup.service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="e")
    issued = setup.service.verify_mfa(
        challenge.raw_challenge, code_at(enrollment.secret, START), correlation_id="v"
    )
    with setup.factory() as session, session.begin():
        session.execute(update(AppUser).where(AppUser.id == AUDITOR_ID).values(role_id=2))

    with pytest.raises(StaffAccessDeniedError):
        setup.service.resolve(issued[1])


def test_suspended_admin_session_is_denied_immediately(setup: Setup) -> None:
    _secret, issued = admin_enroll_and_verify(setup)
    with setup.factory() as session, session.begin():
        session.execute(update(AppUser).where(AppUser.id == ADMIN_ID).values(is_active=False))

    with pytest.raises(StaffAccessDeniedError):
        setup.service.resolve(str(issued[1]))

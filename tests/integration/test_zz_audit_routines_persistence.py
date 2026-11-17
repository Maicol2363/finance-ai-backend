"""PostgreSQL checks for the Security Auditor routines of migration 0022."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from finance_ai.audit.policy import AuditFilters, normalize_period
from finance_ai.audit.report_service import AuditReportService, ProcedureExportRecorder
from finance_ai.audit.service import AccessDenialRecorder, SecurityAuditWriter
from finance_ai.auth.session_policy import SystemClock
from finance_ai.core.config import get_settings
from finance_ai.core.errors import StaffAccessDeniedError
from finance_ai.db.models.admin import AdminTotpFactor, StaffInvitation, StaffMfaChallenge
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.staff_auth.schemas import StaffSessionUser

pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")
CHECK_VIOLATION = "23514"
INSUFFICIENT_PRIVILEGE = "42501"
EXPORT = text(
    "CALL sp_audit_record_report_export("
    ":auditor, :report, 'corr-export', now(), CAST(:context AS jsonb))"
)
DENIED = text(
    "CALL sp_audit_record_access_denied(:actor, :resource, :reason, 'corr-denied', now())"
)


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
    # Exercise the 0022 downgrade/upgrade pair on the isolated test database.
    alembic_command.downgrade(config, "0021_audit_actor_role")
    assert routines() == set()
    alembic_command.upgrade(config, "head")
    assert routines() == {
        "admin_totp_factor_replay_guard",
        "sp_audit_record_access_denied",
        "sp_audit_record_report_export",
        "staff_mfa_challenge_one_time_guard",
    }
    with get_session_factory()() as session:
        seed_shared_data(session)


def routines() -> set[str]:
    with get_session_factory()() as session:
        names = session.scalars(
            text(
                "SELECT tgname FROM pg_trigger WHERE tgname IN "
                "('staff_mfa_challenge_one_time_guard', 'admin_totp_factor_replay_guard') "
                "UNION SELECT proname FROM pg_proc WHERE proname IN "
                "('sp_audit_record_report_export', 'sp_audit_record_access_denied')"
            )
        )
        return set(names)


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


def create_account(role: str, *, activated: bool = False) -> int:
    with get_session_factory()() as session:
        roles = dict(session.execute(select(Role.name, Role.id)).all())
        currency = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency is not None
        account = AppUser(
            email=f"{role}-{uuid4()}@example.test",
            password_hash="integration-hash",
            role_id=roles[role],
            preferred_currency_id=currency,
        )
        session.add(account)
        session.flush()
        if activated:
            now = datetime.now(UTC)
            session.add(
                StaffInvitation(
                    email=account.email,
                    role_id=account.role_id,
                    token_hash=uuid4().hex + uuid4().hex,
                    created_by_admin_user_id=account.id,
                    created_at=now - timedelta(minutes=2),
                    expires_at=now + timedelta(hours=1),
                    consumed_at=now - timedelta(minutes=1),
                    activated_user_id=account.id,
                )
            )
        session.commit()
        return account.id


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def events(event_type: str) -> list[SecurityAuditEvent]:
    with get_session_factory()() as session:
        return list(
            session.scalars(
                select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == event_type)
            )
        )


# --- T1: staff_mfa_challenge_one_time_guard --------------------------------------------------


def new_challenge(user_id: int) -> int:
    now = datetime.now(UTC)
    with get_session_factory()() as session, session.begin():
        challenge = StaffMfaChallenge(
            user_id=user_id,
            token_hash=uuid4().hex + uuid4().hex,
            created_at=now,
            expires_at=now + timedelta(minutes=5),
            failed_attempts=0,
        )
        session.add(challenge)
        session.flush()
        return challenge.id


def change_challenge(challenge_id: int, **values: object) -> None:
    with get_session_factory()() as session, session.begin():
        session.execute(
            update(StaffMfaChallenge).where(StaffMfaChallenge.id == challenge_id).values(**values)
        )


def test_postgres_mfa_challenge_is_one_time_and_keeps_its_identity() -> None:
    auditor = create_account("security_auditor")
    challenge = new_challenge(auditor)
    # The normal lifecycle: a failed attempt, then consumption.
    change_challenge(challenge, failed_attempts=1)
    change_challenge(challenge, consumed_at=datetime.now(UTC))

    for values in (
        {"consumed_at": None},
        {"consumed_at": datetime.now(UTC) + timedelta(seconds=5)},
        {"failed_attempts": 2},
    ):
        with pytest.raises(DBAPIError) as reopened:
            change_challenge(challenge, **values)
        assert sqlstate(reopened.value) == CHECK_VIOLATION

    fresh = new_challenge(auditor)
    change_challenge(fresh, failed_attempts=3)
    for values in (
        {"failed_attempts": 1},
        {"expires_at": datetime.now(UTC) + timedelta(days=1)},
        {"user_id": create_account("security_auditor")},
    ):
        with pytest.raises(DBAPIError) as changed:
            change_challenge(fresh, **values)
        assert sqlstate(changed.value) == CHECK_VIOLATION


# --- T2: admin_totp_factor_replay_guard ------------------------------------------------------


def new_factor(user_id: int) -> int:
    with get_session_factory()() as session, session.begin():
        factor = AdminTotpFactor(admin_user_id=user_id, secret_ciphertext=b"ciphertext")
        session.add(factor)
        session.flush()
        return factor.id


def change_factor(factor_id: int, **values: object) -> None:
    with get_session_factory()() as session, session.begin():
        session.execute(
            update(AdminTotpFactor).where(AdminTotpFactor.id == factor_id).values(**values)
        )


def test_postgres_totp_factor_never_accepts_a_step_twice_or_comes_back() -> None:
    auditor = create_account("security_auditor")
    factor = new_factor(auditor)
    now = datetime.now(UTC)
    # The normal lifecycle: enrollment, later codes, then disabling.
    change_factor(factor, state="active", activated_at=now, last_accepted_time_step=100)
    change_factor(factor, last_accepted_time_step=101)

    for values in (
        {"last_accepted_time_step": 100},
        {"last_accepted_time_step": None},
        {"secret_ciphertext": b"another-secret"},
        {"state": "pending", "activated_at": None},
    ):
        with pytest.raises(DBAPIError) as replay:
            change_factor(factor, **values)
        assert sqlstate(replay.value) == CHECK_VIOLATION

    change_factor(factor, state="disabled", activated_at=None)
    with pytest.raises(DBAPIError) as revived:
        change_factor(factor, state="active", activated_at=now)
    assert sqlstate(revived.value) == CHECK_VIOLATION

    pending = new_factor(auditor)
    change_factor(pending, state="disabled")


# --- P1: sp_audit_record_report_export -------------------------------------------------------


def call_export(auditor_id: int, context: object | None = None) -> None:
    with get_session_factory()() as session, session.begin():
        session.execute(
            EXPORT,
            {
                "auditor": auditor_id,
                "report": uuid4(),
                "context": json.dumps({"event_count": "0"} if context is None else context),
            },
        )


def test_postgres_report_export_records_only_for_a_current_authorized_auditor() -> None:
    auditor = create_account("security_auditor", activated=True)
    call_export(auditor)
    [recorded] = events("audit.report_exported")
    assert recorded.actor_id == str(auditor)
    assert recorded.actor_role == "security_auditor"
    assert recorded.target_resource_type == "audit_report"
    assert recorded.result == "success"
    assert recorded.context == {"event_count": "0"}

    agent = create_account("support_agent", activated=True)
    unactivated = create_account("security_auditor")
    suspended = create_account("security_auditor", activated=True)
    with get_session_factory()() as session, session.begin():
        session.execute(update(AppUser).where(AppUser.id == suspended).values(is_active=False))
    for denied in (agent, unactivated, suspended, 999_999):
        with pytest.raises(DBAPIError) as refused:
            call_export(denied)
        assert sqlstate(refused.value) == INSUFFICIENT_PRIVILEGE
    with pytest.raises(DBAPIError) as malformed:
        call_export(auditor, context=["not", "an", "object"])
    assert sqlstate(malformed.value) == CHECK_VIOLATION
    assert len(events("audit.report_exported")) == 1


def test_postgres_report_delivery_is_blocked_when_authority_ends_before_recording() -> None:
    auditor = create_account("security_auditor", activated=True)
    factory = get_session_factory()
    service = AuditReportService(
        factory,
        SecurityAuditWriter(),
        SystemClock(),
        max_events=2_000,
        export_recorder=ProcedureExportRecorder(factory),
    )
    today = date.today()
    period = normalize_period(today - timedelta(days=1), today, "UTC", max_days=90)
    user = StaffSessionUser(id=auditor, email="auditor@example.test", role="security_auditor")

    report = service.export(
        auditor=user,
        period=period,
        filters=AuditFilters(),
        correlation_id="report-ok",
        authorize_delivery=lambda: user,
    )
    assert report.content.startswith(b"%PDF")
    assert [event.target_resource_id for event in events("audit.report_exported")] == [
        str(report.report_id)
    ]

    def suspend_then_authorize() -> StaffSessionUser:
        # The session check passes, but the account is suspended before the evidence is written.
        with factory() as session, session.begin():
            session.execute(update(AppUser).where(AppUser.id == auditor).values(is_active=False))
        return user

    with pytest.raises(StaffAccessDeniedError):
        service.export(
            auditor=user,
            period=period,
            filters=AuditFilters(),
            correlation_id="report-denied",
            authorize_delivery=suspend_then_authorize,
        )
    assert len(events("audit.report_exported")) == 1


# --- P2: sp_audit_record_access_denied -------------------------------------------------------


def test_postgres_access_denial_has_a_fixed_shape_from_enumerated_values() -> None:
    agent = create_account("support_agent")
    AccessDenialRecorder(get_session_factory()).record(
        actor_id=agent,
        resource="security_audit",
        reason="role_not_permitted",
        correlation_id="denied-1",
    )
    [denial] = events("access.denied")
    assert denial.actor_id == str(agent)
    assert denial.actor_role == "support_agent"
    assert denial.result == "denied"
    assert denial.target_resource_type == "protected_resource"
    assert denial.target_resource_id == "security_audit"
    assert denial.correlation_id == "denied-1"
    assert denial.context == {"reason": "role_not_permitted", "resource": "security_audit"}

    # An unapproved value is refused by the database; the recorder logs it and the request
    # stays denied without raising.
    AccessDenialRecorder(get_session_factory()).record(
        actor_id=agent,
        resource="GET /secret?token=abc",
        reason="role_not_permitted",
        correlation_id="denied-2",
    )
    with pytest.raises(DBAPIError) as refused, get_session_factory()() as session:
        session.execute(
            DENIED, {"actor": agent, "resource": "security_audit", "reason": "free text"}
        )
    assert sqlstate(refused.value) == CHECK_VIOLATION
    assert len(events("access.denied")) == 1

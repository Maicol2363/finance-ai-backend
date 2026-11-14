"""PostgreSQL checks for Security Auditor MFA, read queries and report export (AUD-01..04)."""

from __future__ import annotations

import base64
import os
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pyotp
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from finance_ai.audit.policy import AuditFilters, normalize_period
from finance_ai.audit.query_service import AuditQueryService
from finance_ai.audit.report_service import AuditReportService
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.session_policy import SystemClock
from finance_ai.core.config import get_settings
from finance_ai.core.errors import (
    MfaChallengeInvalidError,
    MfaCodeInvalidError,
    StaffAccessDeniedError,
    StaffSessionExistsError,
)
from finance_ai.db.models.admin import AdminTotpFactor, StaffInvitation
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import UserSession
from finance_ai.db.repositories.sessions import (
    SQLAlchemySessionRepository,
    SQLAlchemySessionUserRepository,
)
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.staff_auth.mfa import StaffMfaPolicy, TotpSecretCipher, TotpVerifier
from finance_ai.staff_auth.schemas import StaffLoginCommand, StaffSessionUser
from finance_ai.staff_auth.service import StaffMfaChallengeIssued, StaffSessionService

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


class IntegrationPasswordHasher:
    def verify_real_or_dummy(self, password_hash: str | None, candidate: str) -> bool:
        return password_hash == "integration-hash" and candidate == "correct-password"


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


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
    # Exercise the 0019 downgrade/upgrade pair on the isolated test database.
    alembic_command.downgrade(config, "0018_staff_foundation")
    assert "staff_mfa_challenge" not in inspect(get_session_factory().kw["bind"]).get_table_names()
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
    yield


def mfa_policy() -> StaffMfaPolicy:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    return StaffMfaPolicy(
        cipher=TotpSecretCipher.from_encoded(key),
        verifier=TotpVerifier(),
        issuer="FinanciAI",
        challenge_minutes=5,
        max_attempts=5,
    )


def create_people() -> dict[str, tuple[int, str]]:
    people: dict[str, tuple[int, str]] = {}
    with get_session_factory()() as session:
        roles = dict(session.execute(select(Role.name, Role.id)).all())
        currency = session.scalar(select(Currency.id).where(Currency.code == "COP"))
        assert currency is not None
        for key, role in (
            ("admin", "admin"),
            ("auditor", "security_auditor"),
            ("customer", "user"),
        ):
            user = AppUser(
                email=f"{key}-{uuid4()}@example.test",
                password_hash="integration-hash",
                role_id=roles[role],
                preferred_currency_id=currency,
            )
            session.add(user)
            session.flush()
            people[key] = (user.id, user.email)
        now = datetime.now(UTC)
        session.add(
            StaffInvitation(
                email=people["auditor"][1],
                role_id=roles["security_auditor"],
                token_hash=uuid4().hex + uuid4().hex,
                created_by_admin_user_id=people["admin"][0],
                created_at=now,
                expires_at=now + timedelta(hours=1),
                consumed_at=now,
                activated_user_id=people["auditor"][0],
            )
        )
        session.commit()
    return people


def staff_service(policy: StaffMfaPolicy) -> StaffSessionService:
    return StaffSessionService(
        session_factory=get_session_factory(),
        password_hasher=IntegrationPasswordHasher(),  # type: ignore[arg-type]
        credential_codec=CredentialCodec(),
        clock=SystemClock(),
        audit_writer=SecurityAuditWriter(),
        mfa=policy,
    )


def login(service: StaffSessionService, email: str) -> StaffMfaChallengeIssued:
    outcome = service.login(
        StaffLoginCommand(email=email, password="correct-password"), correlation_id="pg-login"
    )
    assert isinstance(outcome, StaffMfaChallengeIssued)
    return outcome


def test_postgres_auditor_mfa_flow_locks_factor_and_issues_verified_session() -> None:
    people = create_people()
    service = staff_service(mfa_policy())
    challenge = login(service, people["auditor"][1])
    enrollment = service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="pg")
    user, access, refresh, *_ = service.verify_mfa(
        challenge.raw_challenge, pyotp.TOTP(enrollment.secret).now(), correlation_id="pg"
    )
    assert user.role == "security_auditor"
    assert service.resolve(access).id == people["auditor"][0]
    with get_session_factory()() as session:
        browser_session = session.scalar(select(UserSession))
        factor = session.scalar(select(AdminTotpFactor))
        events = list(
            session.scalars(
                select(SecurityAuditEvent.event_type).order_by(SecurityAuditEvent.occurred_at)
            )
        )
    assert browser_session is not None and browser_session.mfa_verified_at is not None
    assert factor is not None and factor.state == "active"
    assert events == ["auth.mfa_enrolled", "auth.mfa_success", "auth.login_success"]
    with pytest.raises(StaffSessionExistsError):
        login(service, people["auditor"][1])
    del refresh


def test_postgres_concurrent_verification_accepts_a_code_only_once() -> None:
    people = create_people()
    service = staff_service(mfa_policy())
    challenge = login(service, people["auditor"][1])
    enrollment = service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="pg")
    code = pyotp.TOTP(enrollment.secret).now()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            service.verify_mfa(challenge.raw_challenge, code, correlation_id="race")
            result = "verified"
        except (MfaChallengeInvalidError, MfaCodeInvalidError, StaffSessionExistsError) as exc:
            result = type(exc).__name__
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["MfaChallengeInvalidError", "verified"]
    with get_session_factory()() as session:
        assert len(list(session.scalars(select(UserSession.id)))) == 1


def test_postgres_suspension_denies_existing_auditor_session_immediately() -> None:
    people = create_people()
    service = staff_service(mfa_policy())
    challenge = login(service, people["auditor"][1])
    enrollment = service.start_mfa_enrollment(challenge.raw_challenge, correlation_id="pg")
    _user, access, *_ = service.verify_mfa(
        challenge.raw_challenge, pyotp.TOTP(enrollment.secret).now(), correlation_id="pg"
    )
    with get_session_factory()() as session:
        session.execute(
            update(AppUser).where(AppUser.id == people["auditor"][0]).values(is_active=False)
        )
        session.commit()
    with pytest.raises(StaffAccessDeniedError):
        service.resolve(access)


def test_postgres_queries_bucket_by_reporting_zone_and_page_stably() -> None:
    period = normalize_period(date(2026, 10, 1), date(2026, 10, 3), "America/Bogota", max_days=90)
    tied = datetime(2026, 10, 2, 12, tzinfo=UTC)
    with get_session_factory()() as session:
        for _ in range(4):
            session.add(
                SecurityAuditEvent(
                    event_type="access.denied",
                    actor_id="9",
                    occurred_at=tied,
                    result="denied",
                    context={"reason": "role_not_permitted"},
                )
            )
        session.add(
            SecurityAuditEvent(
                event_type="auth.mfa_failed",
                actor_id="9",
                target_resource_type="account",
                target_resource_id="9",
                occurred_at=datetime(2026, 10, 3, 4, 30, tzinfo=UTC),  # Oct 2 in Bogota
                result="failure",
                context={},
            )
        )
        session.commit()
    service = AuditQueryService(
        get_session_factory(),
        FixedClock(datetime(2026, 10, 10, tzinfo=UTC)),
        max_query_days=90,
        max_report_events=2_000,
    )
    seen = []
    cursor = None
    while True:
        page = service.list_events(period, AuditFilters(), cursor=cursor, limit=2)
        seen.extend(item.event_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 5

    summary = service.summary(period, AuditFilters())
    by_day = {item.date: item.count for item in summary.by_day}
    assert by_day == {date(2026, 10, 1): 0, date(2026, 10, 2): 5, date(2026, 10, 3): 0}
    metrics = {item.key: item.count for item in summary.metrics}
    assert metrics["access_denied"] == 4 and metrics["failed_mfa"] == 1

    investigation = service.investigate_account("9", period, cursor=None, limit=10)
    assert [item.match for item in investigation.items][-1] == "actor_and_target"
    assert len(investigation.items) == 5


def test_postgres_report_export_is_recorded_and_evidence_is_append_only() -> None:
    period = normalize_period(date(2026, 10, 1), date(2026, 10, 3), "America/Bogota", max_days=90)
    with get_session_factory()() as session:
        session.add(
            SecurityAuditEvent(
                event_type="staff.login",
                actor_id="3",
                occurred_at=datetime(2026, 10, 2, 15, tzinfo=UTC),
                result="success",
                context={},
            )
        )
        session.commit()
    auditor = StaffSessionUser(id=77, email="auditor@example.test", role="security_auditor")
    report = AuditReportService(
        get_session_factory(),
        SecurityAuditWriter(),
        FixedClock(datetime(2026, 10, 10, tzinfo=UTC)),
        max_events=2_000,
    ).export(
        auditor=auditor,
        period=period,
        filters=AuditFilters(),
        correlation_id="pg-report",
        authorize_delivery=lambda: auditor,
    )
    assert report.event_count == 1 and report.content.startswith(b"%PDF")
    with get_session_factory()() as session:
        exported = session.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.event_type == "audit.report_exported"
            )
        )
        assert exported is not None and exported.context["event_count"] == "1"
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                update(SecurityAuditEvent)
                .where(SecurityAuditEvent.event_id == exported.event_id)
                .values(result="failure")
            )
        session.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                text("DELETE FROM security_audit_event WHERE event_id = :id"),
                {"id": exported.event_id},
            )
        session.rollback()


def test_postgres_customer_api_excludes_security_auditor_principals() -> None:
    people = create_people()
    codec = CredentialCodec()
    now = datetime.now(UTC)
    hashes = {}
    with get_session_factory()() as session:
        for key in ("auditor", "customer"):
            credential = codec.issue()
            hashes[key] = credential.token_hash
            session.add(
                UserSession(
                    user_id=people[key][0],
                    platform="android",
                    access_token_hash=credential.token_hash,
                    access_expires_at=now + timedelta(minutes=10),
                    idle_expires_at=now + timedelta(days=1),
                    absolute_expires_at=now + timedelta(days=2),
                    last_renewed_at=now,
                )
            )
        session.commit()
        users = SQLAlchemySessionUserRepository(session)
        record = users.find_auth_by_email(people["auditor"][1])
        assert record is not None and record.role_name == "security_auditor"
        sessions = SQLAlchemySessionRepository(session)
        assert (
            sessions.find_principal_by_access_hash(access_token_hash=hashes["auditor"], now=now)
            is None
        )
        customer = sessions.find_principal_by_access_hash(
            access_token_hash=hashes["customer"], now=now
        )
        assert customer is not None and customer.user_id == people["customer"][0]

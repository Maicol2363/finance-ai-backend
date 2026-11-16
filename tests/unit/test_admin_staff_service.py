"""ADM-03 invitations, staff activation and ADM-04 role changes (SQLite unit level)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.admin.staff_service import AdminStaffService
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.passwords import PasswordHasher, UnsafePasswordProvider
from finance_ai.core.errors import (
    AccountStateConflictError,
    ActivationPasswordInvalidError,
    AdminAccountNotFoundError,
    AdminRequestInvalidError,
    AdminSelfActionError,
    AuditUnavailableError,
    RoleChangeNotAllowedError,
    StaffActivationInvalidError,
    StaffEmailUnavailableError,
)
from finance_ai.db.base import Base
from finance_ai.db.models.admin import (
    AdminTotpFactor,
    StaffInvitation,
    StaffMfaChallenge,
)
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import RenewableCredential, UserSession
from finance_ai.staff_auth.activation import StaffActivationService
from finance_ai.staff_auth.schemas import StaffLoginCommand
from finance_ai.staff_auth.service import StaffMfaChallengeIssued, StaffSessionService
from tests.unit.test_staff_auditor_auth_service import build_policy

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
ADMIN, AGENT, AUDITOR, CUSTOMER, OTHER_ADMIN = 1, 2, 3, 4, 5
GOOD_PASSWORD = "a-long-and-unusual-passphrase"


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


@dataclass
class Env:
    factory: sessionmaker[Session]
    clock: Clock
    staff: AdminStaffService
    activation: StaffActivationService
    hasher: PasswordHasher


def build_env(tmp_path: Path, writer: SecurityAuditWriter | None = None) -> tuple[Env, object]:
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
        ],  # type: ignore[arg-type]
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
            (ADMIN, "admin@example.test", 2),
            (AGENT, "agent@example.test", 3),
            (AUDITOR, "auditor@example.test", 4),
            (CUSTOMER, "customer@example.com", 1),
            (OTHER_ADMIN, "admin2@example.test", 2),
        ):
            session.add(
                AppUser(
                    id=user_id,
                    email=email,
                    password_hash="existing-hash",
                    role_id=role_id,
                    preferred_currency_id=1,
                    is_active=True,
                )
            )
    clock = Clock()
    codec = CredentialCodec()
    hasher = PasswordHasher(memory_kib=19_456, time_cost=2, parallelism=1)
    unsafe_path = tmp_path / "common.txt"
    unsafe_path.write_text("password123\n", encoding="utf-8")
    audit = writer or SecurityAuditWriter()
    staff = AdminStaffService(factory, audit, codec, clock, invitation_lifetime=timedelta(hours=48))
    activation = StaffActivationService(
        session_factory=factory,
        password_hasher=hasher,
        unsafe_passwords=UnsafePasswordProvider(unsafe_path),
        credential_codec=codec,
        clock=clock,
        audit_writer=audit,
    )
    return Env(factory, clock, staff, activation, hasher), engine


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    built, engine = build_env(tmp_path)
    yield built
    engine.dispose()  # type: ignore[attr-defined]


def invite(env: Env, email: str = "New.Agent@Example.com", role: str = "support_agent"):  # type: ignore[no-untyped-def]
    return env.staff.create_invitation(
        actor_id=ADMIN,
        email=email,
        role=role,
        correlation_id="corr-invite",  # type: ignore[arg-type]
    )


def activate(env: Env, token: str, password: str = GOOD_PASSWORD, confirmation: str | None = None):  # type: ignore[no-untyped-def]
    env.activation.activate(
        token=token,
        password=password,
        password_confirmation=password if confirmation is None else confirmation,
        correlation_id="corr-activate",
    )


def events(env: Env) -> list[SecurityAuditEvent]:
    with env.factory() as session:
        return list(
            session.scalars(select(SecurityAuditEvent).order_by(SecurityAuditEvent.occurred_at))
        )


# --- ADM-03: invitation ------------------------------------------------------------------------


def test_invitation_stores_only_a_token_hash_and_returns_the_token_once(env: Env) -> None:
    created = invite(env)

    assert created.email == "new.agent@example.com" and created.role == "support_agent"
    assert created.expires_at == NOW + timedelta(hours=48)
    assert len(created.activation_token) == 43
    with env.factory() as session:
        stored = session.scalar(select(StaffInvitation))
        assert stored is not None
        assert stored.token_hash == CredentialCodec().hash_presented(created.activation_token)
        assert created.activation_token not in (stored.token_hash, stored.email)
        assert stored.consumed_at is None and stored.activated_user_id is None
        assert session.scalar(select(AppUser).where(AppUser.email == stored.email)) is None
    (audit,) = events(env)
    assert audit.event_type == "staff.created" and audit.result == "success"
    assert audit.actor_id == str(ADMIN) and audit.target_resource_type == "staff_invitation"
    assert audit.context == {"new_role": "support_agent"}
    assert created.activation_token not in str(audit.context)


@pytest.mark.parametrize("role", ["admin", "user", "root", ""])
def test_invitation_refuses_any_role_outside_the_two_portal_staff_roles(
    env: Env, role: str
) -> None:
    with pytest.raises(AdminRequestInvalidError):
        invite(env, role=role)

    with env.factory() as session:
        assert session.scalar(select(StaffInvitation)) is None


@pytest.mark.parametrize("email", ["", "no-at", "two@@example.test", "a b@example.test"])
def test_invitation_validates_the_email(env: Env, email: str) -> None:
    with pytest.raises(AdminRequestInvalidError):
        invite(env, email=email)


def test_invitation_rejects_existing_accounts_and_pending_invitations(env: Env) -> None:
    with pytest.raises(StaffEmailUnavailableError):
        invite(env, email="CUSTOMER@example.com")
    invite(env, email="fresh@example.com")
    with pytest.raises(StaffEmailUnavailableError):
        invite(env, email="fresh@example.com", role="security_auditor")


def test_an_expired_unused_invitation_does_not_block_a_new_one(env: Env) -> None:
    invite(env, email="later@example.com")
    env.clock.value = NOW + timedelta(hours=49)

    second = invite(env, email="later@example.com")

    assert second.expires_at == env.clock.value + timedelta(hours=48)


def test_invitation_is_not_created_when_its_audit_event_cannot_be_written(tmp_path: Path) -> None:
    class FailingWriter(SecurityAuditWriter):
        def append(self, session: Session, **kwargs: object) -> None:  # type: ignore[override]
            raise RuntimeError("audit store unavailable")

    built, engine = build_env(tmp_path, FailingWriter())
    try:
        with pytest.raises(AuditUnavailableError):
            invite(built)
        with built.factory() as session:
            assert session.scalar(select(StaffInvitation)) is None
    finally:
        engine.dispose()  # type: ignore[attr-defined]


# --- ADM-03: activation ------------------------------------------------------------------------


def test_activation_creates_the_account_once_with_the_chosen_password_and_audits_it(
    env: Env,
) -> None:
    token = invite(env).activation_token

    activate(env, token)

    with env.factory() as session:
        account = session.scalar(select(AppUser).where(AppUser.email == "new.agent@example.com"))
        assert account is not None and account.is_active and account.role_id == 3
        assert account.password_hash.startswith("$argon2")
        assert env.hasher.verify(account.password_hash, GOOD_PASSWORD)
        invitation = session.scalar(select(StaffInvitation))
        assert invitation is not None and invitation.activated_user_id == account.id
        assert invitation.consumed_at is not None
    activated = events(env)[-1]
    assert activated.event_type == "staff.activated" and activated.actor_id == str(account.id)
    assert activated.target_resource_type == "account"
    assert activated.context == {"new_role": "support_agent"}
    assert GOOD_PASSWORD not in str(activated.context) and token not in str(activated.context)


def test_a_replayed_activation_token_fails_generically_and_creates_nothing_more(
    env: Env,
) -> None:
    token = invite(env).activation_token
    activate(env, token)

    with pytest.raises(StaffActivationInvalidError):
        activate(env, token, password="another-strong-passphrase")

    with env.factory() as session:
        assert len(list(session.scalars(select(AppUser)))) == 6


def test_expired_unknown_and_malformed_tokens_all_fail_the_same_way(env: Env) -> None:
    token = invite(env).activation_token
    env.clock.value = NOW + timedelta(hours=48, seconds=1)

    for presented in (token, CredentialCodec().issue().raw, "not-a-token", "", "x" * 64):
        with pytest.raises(StaffActivationInvalidError):
            activate(env, presented)

    with env.factory() as session:
        assert (
            session.scalar(select(AppUser).where(AppUser.email == "new.agent@example.com")) is None
        )


@pytest.mark.parametrize("presented", ["valid", "unknown", "malformed", "expired"])
def test_a_rejected_password_is_reported_the_same_whatever_the_token(
    env: Env, presented: str
) -> None:
    """The password policy is checked before the token, so it cannot be used as a token oracle."""
    token = invite(env).activation_token
    if presented == "expired":
        env.clock.value = NOW + timedelta(hours=48, seconds=1)
    candidate = {
        "valid": token,
        "unknown": CredentialCodec().issue().raw,
        "malformed": "not-a-token",
        "expired": token,
    }[presented]

    with pytest.raises(ActivationPasswordInvalidError) as failure:
        activate(env, candidate, password="short")

    assert "password" in (failure.value.field_errors or {})


@pytest.mark.parametrize(
    ("password", "confirmation", "field"),
    [
        ("short", None, "password"),
        ("x" * 129, None, "password"),
        ("password123", None, "password"),
        (GOOD_PASSWORD, "different-passphrase", "password_confirmation"),
    ],
)
def test_unacceptable_passwords_do_not_consume_the_token(
    env: Env, password: str, confirmation: str | None, field: str
) -> None:
    token = invite(env).activation_token

    with pytest.raises(ActivationPasswordInvalidError) as failure:
        activate(env, token, password=password, confirmation=confirmation)

    assert field in (failure.value.field_errors or {})
    activate(env, token)  # still usable with an acceptable password


def test_activation_fails_when_the_email_was_taken_in_the_meantime(env: Env) -> None:
    token = invite(env, email="race@example.com").activation_token
    with env.factory() as session, session.begin():
        session.add(
            AppUser(
                email="race@example.com",
                password_hash="x",
                role_id=1,
                preferred_currency_id=1,
                is_active=True,
            )
        )

    with pytest.raises(StaffActivationInvalidError):
        activate(env, token)

    with env.factory() as session:
        invitation = session.scalar(select(StaffInvitation))
        assert invitation is not None and invitation.consumed_at is None


def test_activation_keeps_the_token_unused_when_its_audit_event_fails(tmp_path: Path) -> None:
    holder: dict[str, bool] = {"fail": False}

    class SwitchableWriter(SecurityAuditWriter):
        def append(self, session: Session, **kwargs: object) -> None:  # type: ignore[override]
            if holder["fail"]:
                raise RuntimeError("audit store unavailable")
            super().append(session, **kwargs)  # type: ignore[arg-type]

    built, engine = build_env(tmp_path, SwitchableWriter())
    try:
        token = invite(built).activation_token
        holder["fail"] = True
        with pytest.raises(AuditUnavailableError):
            activate(built, token)
        holder["fail"] = False
        with built.factory() as session:
            assert (
                session.scalar(select(AppUser).where(AppUser.email == "new.agent@example.com"))
                is None
            )
        activate(built, token)
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_activated_staff_can_sign_in_but_an_auditor_must_complete_mfa(env: Env) -> None:
    agent_token = invite(env, email="agent2@example.com", role="support_agent").activation_token
    auditor_token = invite(
        env, email="auditor2@example.com", role="security_auditor"
    ).activation_token
    activate(env, agent_token)
    activate(env, auditor_token)
    service = StaffSessionService(
        session_factory=env.factory,
        password_hasher=env.hasher,
        credential_codec=CredentialCodec(),
        clock=env.clock,
        audit_writer=SecurityAuditWriter(),
        mfa=build_policy(),
    )

    agent = service.login(
        StaffLoginCommand(email="agent2@example.com", password=GOOD_PASSWORD),
        correlation_id="login-agent",
    )
    auditor = service.login(
        StaffLoginCommand(email="auditor2@example.com", password=GOOD_PASSWORD),
        correlation_id="login-auditor",
    )

    assert not isinstance(agent, StaffMfaChallengeIssued) and agent[0].role == "support_agent"
    assert isinstance(auditor, StaffMfaChallengeIssued) and auditor.enrollment_required


# --- ADM-04 ------------------------------------------------------------------------------------


def add_session(env: Env, user_id: int, token_char: str) -> None:
    with env.factory() as session, session.begin():
        session.add(
            UserSession(
                user_id=user_id,
                platform="staff_browser",
                access_token_hash=token_char * 64,
                access_expires_at=NOW + timedelta(minutes=10),
                idle_expires_at=NOW + timedelta(minutes=20),
                absolute_expires_at=NOW + timedelta(hours=4),
                last_renewed_at=NOW,
            )
        )


@pytest.mark.parametrize(
    ("staff_id", "role", "assignable"),
    [
        (AGENT, "support_agent", ["security_auditor"]),
        (AUDITOR, "security_auditor", ["support_agent"]),
        (OTHER_ADMIN, "admin", []),
    ],
)
def test_view_shows_the_primary_role_and_the_allowed_transitions(
    env: Env, staff_id: int, role: str, assignable: list[str]
) -> None:
    view = env.staff.view(staff_id)

    assert view.role == role and view.assignable_roles == assignable
    assert view.account_type == "login_account"


def test_view_does_not_expose_customers_as_staff(env: Env) -> None:
    with pytest.raises(AdminAccountNotFoundError):
        env.staff.view(CUSTOMER)
    with pytest.raises(AdminAccountNotFoundError):
        env.staff.view(999)


def test_role_change_updates_the_single_role_revokes_sessions_and_audits(env: Env) -> None:
    add_session(env, AGENT, "a")
    add_session(env, AUDITOR, "b")

    view = env.staff.change_role(
        actor_id=ADMIN, staff_id=AGENT, new_role="security_auditor", correlation_id="corr-role"
    )

    assert view.role == "security_auditor" and view.assignable_roles == ["support_agent"]
    with env.factory() as session:
        agent = session.get(AppUser, AGENT)
        assert agent is not None and agent.role_id == 4
        sessions = {row.user_id: row for row in session.scalars(select(UserSession))}
        assert sessions[AGENT].revocation_reason == "role_changed"
        assert sessions[AUDITOR].revoked_at is None
    (audit,) = events(env)
    assert audit.event_type == "role.changed" and audit.result == "success"
    assert audit.actor_id == str(ADMIN) and audit.target_resource_id == str(AGENT)
    assert audit.context == {"previous_role": "support_agent", "new_role": "security_auditor"}


def test_role_change_refuses_own_account_even_for_an_administrator(env: Env) -> None:
    with pytest.raises(AdminSelfActionError):
        env.staff.change_role(
            actor_id=ADMIN, staff_id=ADMIN, new_role="support_agent", correlation_id="c"
        )


@pytest.mark.parametrize("target", [OTHER_ADMIN, CUSTOMER])
def test_role_change_never_touches_administrators_or_customers(env: Env, target: int) -> None:
    with pytest.raises(RoleChangeNotAllowedError):
        env.staff.change_role(
            actor_id=ADMIN, staff_id=target, new_role="support_agent", correlation_id="c"
        )

    with env.factory() as session:
        unchanged = session.get(AppUser, target)
        assert unchanged is not None and unchanged.role_id in {1, 2}
    assert events(env) == []


def test_role_change_rejects_no_op_unknown_and_forged_targets(env: Env) -> None:
    with pytest.raises(AccountStateConflictError):
        env.staff.change_role(
            actor_id=ADMIN, staff_id=AGENT, new_role="support_agent", correlation_id="c"
        )
    with pytest.raises(AdminAccountNotFoundError):
        env.staff.change_role(
            actor_id=ADMIN, staff_id=999, new_role="support_agent", correlation_id="c"
        )


def test_role_change_rolls_back_when_the_audit_event_fails(tmp_path: Path) -> None:
    class FailingWriter(SecurityAuditWriter):
        def append(self, session: Session, **kwargs: object) -> None:  # type: ignore[override]
            raise RuntimeError("audit store unavailable")

    built, engine = build_env(tmp_path, FailingWriter())
    try:
        add_session(built, AGENT, "a")
        with pytest.raises(AuditUnavailableError):
            built.staff.change_role(
                actor_id=ADMIN, staff_id=AGENT, new_role="security_auditor", correlation_id="c"
            )
        with built.factory() as session:
            agent = session.get(AppUser, AGENT)
            assert agent is not None and agent.role_id == 3
            stored = session.scalar(select(UserSession))
            assert stored is not None and stored.revoked_at is None
    finally:
        engine.dispose()  # type: ignore[attr-defined]


def test_changed_staff_keep_a_consistent_login_authority_for_their_new_role(env: Env) -> None:
    token = invite(env, email="mover@example.com", role="support_agent").activation_token
    activate(env, token)
    with env.factory() as session:
        mover = session.scalar(select(AppUser).where(AppUser.email == "mover@example.com"))
        assert mover is not None
        mover_id = mover.id
    env.staff.change_role(
        actor_id=ADMIN, staff_id=mover_id, new_role="security_auditor", correlation_id="c"
    )
    service = StaffSessionService(
        session_factory=env.factory,
        password_hasher=env.hasher,
        credential_codec=CredentialCodec(),
        clock=env.clock,
        audit_writer=SecurityAuditWriter(),
        mfa=build_policy(),
    )

    outcome = service.login(
        StaffLoginCommand(email="mover@example.com", password=GOOD_PASSWORD),
        correlation_id="login",
    )

    # The new role's rules apply immediately: an auditor gets an MFA challenge, not a session.
    assert isinstance(outcome, StaffMfaChallengeIssued)

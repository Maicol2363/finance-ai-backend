"""Controlled administrator provisioning: no portal path, MFA before authority, no secrets."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.admin.provisioning import AdminProvisioner, ProvisioningError
from finance_ai.auth.passwords import PasswordHasher, UnsafePasswordProvider
from finance_ai.db.base import Base
from finance_ai.db.models.admin import AdminTotpFactor, StaffMfaChallenge, StaffMfaRecoveryCode
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import RenewableCredential, UserSession

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
PASSWORD = "a-long-and-unusual-passphrase"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[AdminProvisioner, sessionmaker[Session]]]:
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
            AdminTotpFactor.__table__,
            StaffMfaChallenge.__table__,
            StaffMfaRecoveryCode.__table__,
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
    unsafe = tmp_path / "common.txt"
    unsafe.write_text("password123\n", encoding="utf-8")
    yield (
        AdminProvisioner(
            session_factory=factory,
            password_hasher=PasswordHasher(memory_kib=19_456, time_cost=2, parallelism=1),
            unsafe_passwords=UnsafePasswordProvider(unsafe),
        ),
        factory,
    )
    engine.dispose()


def add_user(
    factory: sessionmaker[Session], *, email: str, role_id: int, active: bool = True
) -> int:
    with factory() as session, session.begin():
        user = AppUser(
            email=email,
            password_hash="existing-hash",
            role_id=role_id,
            preferred_currency_id=1,
            is_active=active,
        )
        session.add(user)
        session.flush()
        return user.id


def test_create_admin_hashes_the_password_and_audits_without_secrets(
    env: tuple[AdminProvisioner, sessionmaker[Session]],
) -> None:
    provisioner, factory = env

    result = provisioner.create_admin(email="  Owner@Example.COM ", password=PASSWORD)

    with factory() as session:
        account = session.scalar(select(AppUser))
        assert account is not None and account.id == result.user_id
        assert account.email == "owner@example.com" and account.role_id == 2
        assert account.password_hash.startswith("$argon2") and PASSWORD not in account.password_hash
        audit = session.scalar(select(SecurityAuditEvent))
        assert audit is not None
        assert audit.event_type == "role.changed" and audit.actor_id is None
        assert audit.context == {"previous_role": "none", "new_role": "admin"}
        assert audit.target_resource_type == "account"
        assert PASSWORD not in str(audit.context) and "owner@example.com" not in str(audit.context)


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("not-an-email", PASSWORD),
        ("owner@example.com", "short"),
        ("owner@example.com", "x" * 129),
        ("owner@example.com", "password123"),
    ],
)
def test_create_admin_rejects_invalid_input(
    env: tuple[AdminProvisioner, sessionmaker[Session]], email: str, password: str
) -> None:
    provisioner, factory = env

    with pytest.raises(ProvisioningError):
        provisioner.create_admin(email=email, password=password)

    with factory() as session:
        assert session.scalar(select(AppUser)) is None


def test_create_admin_refuses_a_duplicate_email(
    env: tuple[AdminProvisioner, sessionmaker[Session]],
) -> None:
    provisioner, factory = env
    add_user(factory, email="owner@example.com", role_id=1)

    with pytest.raises(ProvisioningError, match="already exists"):
        provisioner.create_admin(email="OWNER@example.com", password=PASSWORD)


def test_promotion_requires_new_mfa_and_revokes_old_sessions_and_authenticators(
    env: tuple[AdminProvisioner, sessionmaker[Session]],
) -> None:
    provisioner, factory = env
    user_id = add_user(factory, email="auditor@example.com", role_id=4)
    with factory() as session, session.begin():
        session.add(
            UserSession(
                user_id=user_id,
                platform="staff_browser",
                access_token_hash="a" * 64,
                access_expires_at=NOW + timedelta(minutes=10),
                idle_expires_at=NOW + timedelta(minutes=20),
                absolute_expires_at=NOW + timedelta(hours=4),
                last_renewed_at=NOW,
                mfa_verified_at=NOW,
            )
        )
        session.add(
            AdminTotpFactor(
                admin_user_id=user_id,
                secret_ciphertext=b"x",
                state="active",
                activated_at=NOW,
            )
        )

    result = provisioner.promote_to_admin(email="auditor@example.com")

    assert result.action == "promoted"
    with factory() as session:
        assert session.get(AppUser, user_id).role_id == 2  # type: ignore[union-attr]
        stored = session.scalar(select(UserSession))
        assert stored is not None and stored.revoked_at is not None
        factor = session.scalar(select(AdminTotpFactor))
        assert factor is not None and factor.state == "disabled"
        audit = session.scalar(select(SecurityAuditEvent))
        assert audit is not None
        assert audit.context == {"previous_role": "security_auditor", "new_role": "admin"}


@pytest.mark.parametrize(
    ("email", "message"),
    [
        ("nobody@example.com", "No account"),
        ("admin@example.com", "already"),
        ("off@example.com", "suspended"),
    ],
)
def test_promotion_refusals(
    env: tuple[AdminProvisioner, sessionmaker[Session]], email: str, message: str
) -> None:
    provisioner, factory = env
    add_user(factory, email="admin@example.com", role_id=2)
    add_user(factory, email="off@example.com", role_id=1, active=False)

    with pytest.raises(ProvisioningError, match=message):
        provisioner.promote_to_admin(email=email)


def test_reset_mfa_disables_authenticator_revokes_sessions_and_audits(
    env: tuple[AdminProvisioner, sessionmaker[Session]],
) -> None:
    provisioner, factory = env
    user_id = add_user(factory, email="admin@example.com", role_id=2)
    with factory() as session, session.begin():
        session.add(
            AdminTotpFactor(
                admin_user_id=user_id, secret_ciphertext=b"x", state="active", activated_at=NOW
            )
        )
        session.add(StaffMfaRecoveryCode(user_id=user_id, code_hash="b" * 64, created_at=NOW))

    result = provisioner.reset_mfa(email="admin@example.com")

    assert result.action == "mfa_reset"
    with factory() as session:
        factor = session.scalar(select(AdminTotpFactor))
        assert factor is not None and factor.state == "disabled"
        code = session.scalar(select(StaffMfaRecoveryCode))
        assert code is not None and code.consumed_at is not None
        audit = session.scalar(select(SecurityAuditEvent))
        assert audit is not None and audit.event_type == "auth.mfa_recovered"
        assert audit.context == {"reason": "operator_reset"} and audit.actor_id is None


def test_reset_mfa_is_limited_to_authenticator_roles(
    env: tuple[AdminProvisioner, sessionmaker[Session]],
) -> None:
    provisioner, factory = env
    add_user(factory, email="agent@example.com", role_id=3)

    with pytest.raises(ProvisioningError, match="administrators and auditors"):
        provisioner.reset_mfa(email="agent@example.com")

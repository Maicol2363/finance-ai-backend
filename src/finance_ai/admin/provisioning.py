"""Controlled backend provisioning of Administrators (shared prerequisite for ADM/SEC).

Administrators are never created, promoted or demoted through the portal or its API. An
operator with backend access uses ``scripts/admin_ops.py``, which calls these functions.
Every change is audited as ``role.changed`` or ``auth.mfa_recovered`` with no actor (the
operator is not an application account) and no credential in any event or output.

A promoted account has no administrative authority until it completes MFA enrollment: its
sessions, authenticators and recovery codes are invalidated on promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.passwords import PasswordHasher, UnsafePasswordProvider
from finance_ai.auth.policy import RegistrationPolicy
from finance_ai.auth.schemas import RegisterCommand
from finance_ai.core.errors import RegistrationValidationError
from finance_ai.core.roles import ADMIN_ROLE, MFA_REQUIRED_ROLES
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.staff_auth.recovery import invalidate_authenticator

DEFAULT_CURRENCY = "COP"


class ProvisioningError(Exception):
    """Operator-facing failure; the message never contains credentials."""


@dataclass(frozen=True)
class ProvisioningResult:
    user_id: int
    action: str
    sessions_revoked: int = 0


class AdminProvisioner:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        password_hasher: PasswordHasher,
        unsafe_passwords: UnsafePasswordProvider,
        audit_writer: SecurityAuditWriter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._password_hasher = password_hasher
        self._unsafe_passwords = unsafe_passwords
        self._audit = audit_writer or SecurityAuditWriter()

    def create_admin(self, *, email: str, password: str) -> ProvisioningResult:
        """Create a new administrator account with its own password hash."""
        normalized = self._validated_email(email)
        self._validate_password(password)
        correlation_id = _correlation_id()
        password_hash = self._password_hasher.hash(password)
        with self._session_factory() as session, session.begin():
            if session.scalar(select(AppUser.id).where(AppUser.email == normalized)) is not None:
                raise ProvisioningError("An account with this email already exists.")
            role_id = self._role_id(session, ADMIN_ROLE)
            currency_id = session.scalar(
                select(Currency.id).where(Currency.code == DEFAULT_CURRENCY)
            )
            if currency_id is None:
                raise ProvisioningError("Reference data is missing; run the seed command first.")
            account = AppUser(
                email=normalized,
                password_hash=password_hash,
                role_id=role_id,
                preferred_currency_id=currency_id,
                is_active=True,
            )
            session.add(account)
            session.flush()
            self._audit_role_change(session, account.id, "none", correlation_id)
            return ProvisioningResult(user_id=account.id, action="created")

    def promote_to_admin(self, *, email: str) -> ProvisioningResult:
        """Promote an existing active account; it must enroll MFA before any authority."""
        normalized = self._validated_email(email)
        correlation_id = _correlation_id()
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            row = session.execute(
                select(AppUser, Role.name)
                .join(Role, Role.id == AppUser.role_id)
                .where(AppUser.email == normalized)
                .with_for_update(of=AppUser)
            ).one_or_none()
            if row is None:
                raise ProvisioningError("No account uses this email.")
            account, previous_role = row
            if not account.is_active:
                raise ProvisioningError("The account is suspended; reactivate it first.")
            if previous_role == ADMIN_ROLE:
                raise ProvisioningError("The account is already an administrator.")
            invalidate_authenticator(session, account.id, now, session_reason="role_changed")
            account.role_id = self._role_id(session, ADMIN_ROLE)
            session.flush()
            self._audit_role_change(session, account.id, previous_role, correlation_id)
            return ProvisioningResult(user_id=account.id, action="promoted")

    def reset_mfa(self, *, email: str) -> ProvisioningResult:
        """Operator recovery after out-of-band identity verification (no recovery code)."""
        normalized = self._validated_email(email)
        correlation_id = _correlation_id()
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            row = session.execute(
                select(AppUser, Role.name)
                .join(Role, Role.id == AppUser.role_id)
                .where(AppUser.email == normalized)
                .with_for_update(of=AppUser)
            ).one_or_none()
            if row is None:
                raise ProvisioningError("No account uses this email.")
            account, role_name = row
            if role_name not in MFA_REQUIRED_ROLES:
                raise ProvisioningError("Only administrators and auditors use an authenticator.")
            revoked = invalidate_authenticator(session, account.id, now, session_reason="mfa_reset")
            self._audit.append(
                session,
                event_type="auth.mfa_recovered",
                actor_id=None,
                target_id=str(account.id),
                target_type="account",
                result="success",
                correlation_id=correlation_id,
                context={"reason": "operator_reset"},
            )
            return ProvisioningResult(
                user_id=account.id, action="mfa_reset", sessions_revoked=revoked
            )

    def _audit_role_change(
        self, session: Session, user_id: int, previous_role: str, correlation_id: str
    ) -> None:
        self._audit.append(
            session,
            event_type="role.changed",
            actor_id=None,
            target_id=str(user_id),
            target_type="account",
            result="success",
            correlation_id=correlation_id,
            context={"previous_role": previous_role, "new_role": ADMIN_ROLE},
        )

    @staticmethod
    def _role_id(session: Session, name: str) -> int:
        role_id = session.scalar(select(Role.id).where(Role.name == name))
        if role_id is None:
            raise ProvisioningError("Reference data is missing; run the seed command first.")
        return int(role_id)

    @staticmethod
    def _validated_email(email: str) -> str:
        try:
            return (
                RegistrationPolicy()
                .validate(
                    RegisterCommand(email=email, password="x" * 8, password_confirmation="x" * 8)
                )
                .normalized_email
            )
        except RegistrationValidationError as exc:
            raise ProvisioningError("Enter a valid email address.") from exc

    def _validate_password(self, password: str) -> None:
        if not (
            RegistrationPolicy.MIN_PASSWORD_LENGTH
            <= len(password)
            <= RegistrationPolicy.MAX_PASSWORD_LENGTH
        ):
            raise ProvisioningError("The password length is not allowed.")
        try:
            self._unsafe_passwords.reject_if_unsafe(password)
        except RegistrationValidationError as exc:
            raise ProvisioningError("The password is too common; choose another.") from exc


def _correlation_id() -> str:
    return f"admin-ops-{uuid4().hex[:24]}"

"""ADM-03 staff activation: a single-use, expiring token turns an invitation into an account.

The new staff member chooses their own password under the same policy and Argon2 hashing as
public registration. No staff privilege exists before this succeeds: the account row is only
created here. Unknown, malformed, expired and replayed tokens share one generic failure, and
concurrent attempts cannot consume one invitation twice (the row is locked FOR UPDATE and the
consumption pair is enforced by a CHECK constraint).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, lazyload, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.passwords import PasswordHasher, UnsafePasswordProvider
from finance_ai.auth.policy import RegistrationPolicy
from finance_ai.auth.session_policy import Clock
from finance_ai.core.errors import (
    ActivationPasswordInvalidError,
    AuditUnavailableError,
    RegistrationValidationError,
    ServiceUnavailableError,
    StaffActivationInvalidError,
)
from finance_ai.db.models.admin import StaffInvitation
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency

DEFAULT_CURRENCY = "COP"


class StaffActivationService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        password_hasher: PasswordHasher,
        unsafe_passwords: UnsafePasswordProvider,
        credential_codec: CredentialCodec,
        clock: Clock,
        audit_writer: SecurityAuditWriter,
    ) -> None:
        self._factory = session_factory
        self._hasher = password_hasher
        self._unsafe = unsafe_passwords
        self._codec = credential_codec
        self._clock = clock
        self._audit = audit_writer

    def activate(
        self, *, token: str, password: str, password_confirmation: str, correlation_id: str
    ) -> None:
        token_hash = self._codec.try_hash_presented(token)
        if token_hash is None:
            raise StaffActivationInvalidError()
        # Cheap pre-check before the expensive password hash; the locked re-check below decides.
        with self._factory() as session:
            if self._usable(session, token_hash) is None:
                raise StaffActivationInvalidError()
        self._validate_password(password, password_confirmation)
        password_hash = self._hasher.hash(password)

        now = self._clock.now()
        try:
            with self._factory() as session, session.begin():
                # Eager joins on the invitation cannot be locked; lock only the invitation row.
                invitation = session.execute(
                    select(StaffInvitation)
                    .options(lazyload("*"))
                    .where(StaffInvitation.token_hash == token_hash)
                    .with_for_update(of=StaffInvitation)
                ).scalar_one_or_none()
                if (
                    invitation is None
                    or invitation.consumed_at is not None
                    or _aware(invitation.expires_at) <= now
                ):
                    raise StaffActivationInvalidError()
                if session.scalar(
                    select(AppUser.id).where(AppUser.email == invitation.email).limit(1)
                ):
                    raise StaffActivationInvalidError()
                role_name = session.scalar(select(Role.name).where(Role.id == invitation.role_id))
                currency_id = session.scalar(
                    select(Currency.id).where(
                        Currency.code == DEFAULT_CURRENCY, Currency.is_active.is_(True)
                    )
                )
                if role_name is None or currency_id is None:
                    raise ServiceUnavailableError()
                account = AppUser(
                    email=invitation.email,
                    password_hash=password_hash,
                    role_id=invitation.role_id,
                    preferred_currency_id=currency_id,
                    is_active=True,
                )
                session.add(account)
                session.flush()
                invitation.consumed_at = now
                invitation.activated_user_id = account.id
                self._audit.append(
                    session,
                    event_type="staff.activated",
                    actor_id=str(account.id),
                    target_id=str(account.id),
                    target_type="account",
                    result="success",
                    correlation_id=correlation_id,
                    context={"new_role": role_name},
                )
        except StaffActivationInvalidError, ServiceUnavailableError:
            raise
        except Exception as exc:
            raise AuditUnavailableError() from exc

    def _usable(self, session: Session, token_hash: str) -> int | None:
        now = self._clock.now()
        row = session.execute(
            select(StaffInvitation.id, StaffInvitation.expires_at, StaffInvitation.consumed_at)
            .where(StaffInvitation.token_hash == token_hash)
            .limit(1)
        ).one_or_none()
        if row is None or row.consumed_at is not None or _aware(row.expires_at) <= now:
            return None
        return int(row.id)

    def _validate_password(self, password: str, confirmation: str) -> None:
        errors: dict[str, list[str]] = {}
        if not (
            RegistrationPolicy.MIN_PASSWORD_LENGTH
            <= len(password)
            <= RegistrationPolicy.MAX_PASSWORD_LENGTH
        ):
            errors["password"] = [
                f"Password must be {RegistrationPolicy.MIN_PASSWORD_LENGTH} to "
                f"{RegistrationPolicy.MAX_PASSWORD_LENGTH} characters."
            ]
        else:
            try:
                self._unsafe.reject_if_unsafe(password)
            except RegistrationValidationError as exc:
                errors["password"] = (exc.field_errors or {}).get(
                    "password", ["Choose another password."]
                )
        if password != confirmation:
            errors["password_confirmation"] = ["Passwords do not match."]
        if errors:
            raise ActivationPasswordInvalidError(errors)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

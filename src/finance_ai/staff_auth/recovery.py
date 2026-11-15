"""SEC-01 recovery and authenticator replacement for privileged staff (admin, auditor).

Identity is verified with the account password plus either a single-use recovery code
(lost device) or the current authenticator code (voluntary replacement). Success disables
every authenticator, invalidates every recovery code and live session, and opens a fresh
enrollment challenge. Operators without any proof use ``scripts/admin_ops.py reset-mfa``.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session, lazyload, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.passwords import PasswordHasher
from finance_ai.auth.session_policy import Clock
from finance_ai.core.errors import (
    AuditUnavailableError,
    MfaRecoveryInvalidError,
    MfaUnavailableError,
    PasswordConfirmationInvalidError,
)
from finance_ai.core.roles import MFA_REQUIRED_ROLES
from finance_ai.db.models.admin import AdminTotpFactor, StaffMfaChallenge, StaffMfaRecoveryCode
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.sessions import UserSession
from finance_ai.staff_auth.mfa import StaffMfaPolicy
from finance_ai.staff_auth.service import StaffMfaChallengeIssued, staff_activation_is_authoritative

RECOVERY_CODE_COUNT: Final = 10
_ALPHABET: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_CODE_LENGTH: Final = 16
_NORMALIZED = re.compile(rf"^[{_ALPHABET}]{{{_CODE_LENGTH}}}$")


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Random single-use codes with 80 bits of entropy, shown once as XXXX-XXXX-XXXX-XXXX."""
    codes: list[str] = []
    while len(codes) < count:
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
        codes.append("-".join(raw[index : index + 4] for index in range(0, _CODE_LENGTH, 4)))
    return codes


def hash_recovery_code(code: str) -> str | None:
    """SHA-256 of the normalized code; ``None`` when the text cannot be a recovery code."""
    normalized = re.sub(r"[\s-]", "", code).upper()
    if _NORMALIZED.fullmatch(normalized) is None:
        return None
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def invalidate_authenticator(
    session: Session, user_id: int, now: datetime, *, session_reason: str
) -> int:
    """Disable every authenticator, burn recovery codes and challenges, revoke live sessions.

    Returns the number of sessions revoked. Runs in the caller's transaction.
    """
    session.execute(
        update(AdminTotpFactor)
        .where(AdminTotpFactor.admin_user_id == user_id, AdminTotpFactor.state != "disabled")
        .values(state="disabled", activated_at=None)
    )
    session.execute(
        update(StaffMfaRecoveryCode)
        .where(StaffMfaRecoveryCode.user_id == user_id, StaffMfaRecoveryCode.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    session.execute(
        update(StaffMfaChallenge)
        .where(StaffMfaChallenge.user_id == user_id, StaffMfaChallenge.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    revoked = session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now, revocation_reason=session_reason)
    )
    return int(cast("CursorResult[Any]", revoked).rowcount or 0)


class StaffRecoveryService:
    """Recovery codes and authenticator reset; every state change is audited atomically."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        password_hasher: PasswordHasher,
        credential_codec: CredentialCodec,
        clock: Clock,
        audit_writer: SecurityAuditWriter,
        mfa: StaffMfaPolicy | None,
    ) -> None:
        self._session_factory = session_factory
        self._password_hasher = password_hasher
        self._credential_codec = credential_codec
        self._clock = clock
        self._audit_writer = audit_writer
        self._mfa = mfa

    def issue_recovery_codes(
        self, user_id: int, password: str, *, correlation_id: str
    ) -> list[str]:
        """Replace the account's recovery codes after re-confirming the password."""
        now = self._clock.now()
        codes = new_recovery_codes()
        hashes = [hash_recovery_code(code) for code in codes]
        try:
            with self._session_factory() as session, session.begin():
                account = session.execute(
                    select(AppUser).where(AppUser.id == user_id).with_for_update()
                ).scalar_one()
                if not self._password_hasher.verify_real_or_dummy(account.password_hash, password):
                    raise PasswordConfirmationInvalidError()
                session.execute(
                    update(StaffMfaRecoveryCode)
                    .where(
                        StaffMfaRecoveryCode.user_id == user_id,
                        StaffMfaRecoveryCode.consumed_at.is_(None),
                    )
                    .values(consumed_at=now)
                )
                for code_hash in hashes:
                    assert code_hash is not None
                    session.add(
                        StaffMfaRecoveryCode(user_id=user_id, code_hash=code_hash, created_at=now)
                    )
                self._audit_writer.append(
                    session,
                    event_type="auth.mfa_enrolled",
                    actor_id=str(user_id),
                    target_id=str(user_id),
                    target_type="account",
                    result="success",
                    correlation_id=correlation_id,
                    context={"reason": "recovery_codes_issued"},
                )
        except PasswordConfirmationInvalidError:
            raise
        except Exception as exc:
            raise AuditUnavailableError() from exc
        return codes

    def reset_authenticator(
        self,
        *,
        email: str,
        password: str,
        recovery_code: str | None,
        totp_code: str | None,
        correlation_id: str,
    ) -> StaffMfaChallengeIssued:
        """Verify identity, replace the authenticator and open an enrollment challenge."""
        policy = self._mfa
        if policy is None:
            raise MfaUnavailableError()
        normalized_email = email.strip().lower()
        with self._session_factory() as lookup:
            snapshot = lookup.execute(
                select(AppUser.id, AppUser.password_hash).where(AppUser.email == normalized_email)
            ).one_or_none()
        password_ok = self._password_hasher.verify_real_or_dummy(
            snapshot.password_hash if snapshot is not None else None, password
        )
        if snapshot is None or not password_ok:
            self._record_failure(None if snapshot is None else snapshot.id, correlation_id)
            raise MfaRecoveryInvalidError()

        now = self._clock.now()
        challenge = self._credential_codec.issue()
        outcome: str | None = None
        expires_at = now + timedelta(minutes=policy.challenge_minutes)
        try:
            with self._session_factory() as session, session.begin():
                row = session.execute(
                    select(AppUser, Role.name)
                    .join(Role, Role.id == AppUser.role_id)
                    .where(AppUser.id == snapshot.id)
                    .with_for_update(of=AppUser)
                ).one_or_none()
                if row is None:
                    outcome = "invalid"
                else:
                    account, role_name = row
                    eligible = (
                        account.is_active
                        and account.password_hash == snapshot.password_hash
                        and role_name in MFA_REQUIRED_ROLES
                        and staff_activation_is_authoritative(session, account, role_name)
                    )
                    # The proof is consumed only for an eligible account.
                    proof = (
                        self._verify_proof(
                            session,
                            policy,
                            account=account,
                            recovery_code=recovery_code,
                            totp_code=totp_code,
                            now=now,
                        )
                        if eligible
                        else None
                    )
                    if proof is None:
                        outcome = "invalid"
                        self._record_failure_in(session, account.id, correlation_id)
                    else:
                        invalidate_authenticator(
                            session, account.id, now, session_reason="mfa_reset"
                        )
                        session.add(
                            StaffMfaChallenge(
                                user_id=account.id,
                                token_hash=challenge.token_hash,
                                created_at=now,
                                expires_at=expires_at,
                                failed_attempts=0,
                            )
                        )
                        self._audit_writer.append(
                            session,
                            event_type=(
                                "auth.mfa_recovered"
                                if proof == "recovery_code"
                                else "auth.mfa_replaced"
                            ),
                            actor_id=str(account.id),
                            target_id=str(account.id),
                            target_type="account",
                            result="success",
                            correlation_id=correlation_id,
                            context={"reason": proof},
                        )
                        outcome = "reset"
        except Exception as exc:
            raise AuditUnavailableError() from exc
        if outcome != "reset":
            raise MfaRecoveryInvalidError()
        return StaffMfaChallengeIssued(
            raw_challenge=challenge.raw, expires_at=expires_at, enrollment_required=True
        )

    def _verify_proof(
        self,
        session: Session,
        policy: StaffMfaPolicy,
        *,
        account: AppUser,
        recovery_code: str | None,
        totp_code: str | None,
        now: datetime,
    ) -> str | None:
        """Consume the offered proof; exactly one kind may be presented."""
        if (recovery_code is None) == (totp_code is None):
            return None
        if recovery_code is not None:
            code_hash = hash_recovery_code(recovery_code)
            if code_hash is None:
                return None
            stored = session.execute(
                select(StaffMfaRecoveryCode)
                .where(
                    StaffMfaRecoveryCode.user_id == account.id,
                    StaffMfaRecoveryCode.code_hash == code_hash,
                    StaffMfaRecoveryCode.consumed_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if stored is None:
                return None
            stored.consumed_at = now
            return "recovery_code"
        assert totp_code is not None
        factor = session.execute(
            select(AdminTotpFactor)
            .options(lazyload(AdminTotpFactor.admin))
            .where(AdminTotpFactor.admin_user_id == account.id, AdminTotpFactor.state == "active")
            .order_by(AdminTotpFactor.created_at.desc(), AdminTotpFactor.id.desc())
            .limit(1)
            .with_for_update(of=AdminTotpFactor)
        ).scalar_one_or_none()
        if factor is None:
            return None
        try:
            secret = policy.cipher.decrypt(factor.secret_ciphertext, user_id=account.id)
        except ValueError:
            return None
        step = policy.verifier.matching_step(secret, totp_code, now=now)
        if step is None or (
            factor.last_accepted_time_step is not None and step <= factor.last_accepted_time_step
        ):
            return None
        factor.last_accepted_time_step = step
        return "authenticator_replacement"

    def _record_failure(self, user_id: int | None, correlation_id: str) -> None:
        try:
            with self._session_factory() as session, session.begin():
                self._record_failure_in(session, user_id, correlation_id)
        except Exception as exc:
            raise AuditUnavailableError() from exc

    def _record_failure_in(
        self, session: Session, user_id: int | None, correlation_id: str
    ) -> None:
        self._audit_writer.append(
            session,
            event_type="auth.mfa_failed",
            actor_id=None if user_id is None else str(user_id),
            target_id=None if user_id is None else str(user_id),
            target_type="account",
            result="failure",
            correlation_id=correlation_id,
            context={"reason": "recovery_failed"},
        )

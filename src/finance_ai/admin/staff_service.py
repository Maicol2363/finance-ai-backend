"""ADM-03 staff invitations and ADM-04 staff role changes.

The administrator never sees or sets a staff password. An invitation stores only the hash of a
256-bit one-time token; the account is created at activation (see ``staff_auth.activation``).
A role change updates the single primary role, revokes the person's sessions and records
``role.changed`` in the same transaction.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.admin.schemas import (
    PortalStaffRole,
    StaffAccountView,
    StaffInvitationCreated,
)
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.policy import RegistrationPolicy
from finance_ai.auth.schemas import RegisterCommand
from finance_ai.auth.session_policy import Clock
from finance_ai.core.errors import (
    AccountStateConflictError,
    AdminAccountNotFoundError,
    AdminRequestInvalidError,
    AdminSelfActionError,
    AuditUnavailableError,
    RegistrationValidationError,
    RoleChangeNotAllowedError,
    StaffEmailUnavailableError,
)
from finance_ai.core.roles import PORTAL_ASSIGNABLE_STAFF_ROLES, STAFF_PORTAL_ROLES
from finance_ai.db.models.admin import StaffInvitation
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.sessions import UserSession

_ASSIGNABLE: Final[tuple[PortalStaffRole, ...]] = ("support_agent", "security_auditor")


def normalize_staff_email(email: str) -> str:
    """Same normalization and syntax rules as public registration."""
    try:
        return (
            RegistrationPolicy()
            .validate(RegisterCommand(email=email, password="x" * 8, password_confirmation="x" * 8))
            .normalized_email
        )
    except RegistrationValidationError as exc:
        raise AdminRequestInvalidError({"email": ["Enter a valid email address."]}) from exc


class AdminStaffService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        audit_writer: SecurityAuditWriter,
        credential_codec: CredentialCodec,
        clock: Clock,
        *,
        invitation_lifetime: timedelta,
    ) -> None:
        self._factory = session_factory
        self._audit = audit_writer
        self._codec = credential_codec
        self._clock = clock
        self._invitation_lifetime = invitation_lifetime

    # --- ADM-03 ---------------------------------------------------------------------------

    def create_invitation(
        self, *, actor_id: int, email: str, role: PortalStaffRole, correlation_id: str
    ) -> StaffInvitationCreated:
        normalized = normalize_staff_email(email)
        if role not in PORTAL_ASSIGNABLE_STAFF_ROLES:
            raise AdminRequestInvalidError({"role": ["Choose support agent or security auditor."]})
        now = self._clock.now()
        token = self._codec.issue()
        expires_at = now + self._invitation_lifetime
        try:
            with self._factory() as session, session.begin():
                _serialize_by_key(session, f"staff-invitation:{normalized}")
                if self._email_in_use(session, normalized, now):
                    raise StaffEmailUnavailableError()
                role_id = session.scalar(select(Role.id).where(Role.name == role))
                if role_id is None:
                    raise AdminRequestInvalidError({"role": ["This role is not available."]})
                invitation = StaffInvitation(
                    email=normalized,
                    role_id=role_id,
                    token_hash=token.token_hash,
                    created_by_admin_user_id=actor_id,
                    created_at=now,
                    expires_at=expires_at,
                )
                session.add(invitation)
                session.flush()
                self._audit.append(
                    session,
                    event_type="staff.created",
                    actor_id=str(actor_id),
                    target_id=str(invitation.id),
                    target_type="staff_invitation",
                    result="success",
                    correlation_id=correlation_id,
                    context={"new_role": role},
                )
                invitation_id = invitation.id
        except StaffEmailUnavailableError, AdminRequestInvalidError:
            raise
        except Exception as exc:
            raise AuditUnavailableError() from exc
        return StaffInvitationCreated(
            invitation_id=invitation_id,
            email=normalized,
            role=role,
            expires_at=expires_at,
            activation_token=token.raw,
        )

    @staticmethod
    def _email_in_use(session: Session, email: str, now: datetime) -> bool:
        if session.scalar(select(AppUser.id).where(AppUser.email == email).limit(1)) is not None:
            return True
        live = session.scalar(
            select(StaffInvitation.id)
            .where(
                StaffInvitation.email == email,
                StaffInvitation.consumed_at.is_(None),
                StaffInvitation.expires_at > now,
            )
            .limit(1)
        )
        return live is not None

    # --- ADM-04 ---------------------------------------------------------------------------

    def view(self, staff_id: int) -> StaffAccountView:
        with self._factory() as session:
            row = self._staff_row(session, staff_id)
            if row is None:
                raise AdminAccountNotFoundError()
            account, role_name = row
            return _view(account, role_name)

    def change_role(
        self, *, actor_id: int, staff_id: int, new_role: PortalStaffRole, correlation_id: str
    ) -> StaffAccountView:
        if staff_id == actor_id:
            raise AdminSelfActionError()
        now = self._clock.now()
        try:
            with self._factory() as session, session.begin():
                row = session.execute(
                    select(AppUser, Role.name)
                    .join(Role, Role.id == AppUser.role_id)
                    .where(AppUser.id == staff_id)
                    .with_for_update(of=AppUser)
                ).one_or_none()
                if row is None:
                    raise AdminAccountNotFoundError()
                account, previous_role = row
                if previous_role not in PORTAL_ASSIGNABLE_STAFF_ROLES:
                    raise RoleChangeNotAllowedError()
                if previous_role == new_role:
                    raise AccountStateConflictError()
                role_id = session.scalar(select(Role.id).where(Role.name == new_role))
                if role_id is None:
                    raise AdminRequestInvalidError({"role": ["This role is not available."]})
                account.role_id = role_id
                # Trigger T2 revokes live sessions in PostgreSQL; this keeps the guarantee
                # explicit and portable (ADM-04 #7-8).
                session.execute(
                    update(UserSession)
                    .where(UserSession.user_id == staff_id, UserSession.revoked_at.is_(None))
                    .values(revoked_at=now, revocation_reason="role_changed")
                )
                session.flush()
                self._audit.append(
                    session,
                    event_type="role.changed",
                    actor_id=str(actor_id),
                    target_id=str(staff_id),
                    target_type="account",
                    result="success",
                    correlation_id=correlation_id,
                    context={"previous_role": previous_role, "new_role": new_role},
                )
                result = _view(account, new_role)
        except (
            AdminAccountNotFoundError,
            RoleChangeNotAllowedError,
            AccountStateConflictError,
            AdminRequestInvalidError,
        ):
            raise
        except Exception as exc:
            raise AuditUnavailableError() from exc
        return result

    @staticmethod
    def _staff_row(session: Session, staff_id: int) -> tuple[AppUser, str] | None:
        row = session.execute(
            select(AppUser, Role.name)
            .join(Role, Role.id == AppUser.role_id)
            .where(AppUser.id == staff_id, Role.name.in_(STAFF_PORTAL_ROLES))
        ).one_or_none()
        return None if row is None else (row[0], row[1])


def _view(account: AppUser, role_name: str) -> StaffAccountView:
    assignable = [role for role in _ASSIGNABLE if role != role_name]
    return StaffAccountView(
        id=account.id,
        email=account.email,
        role=role_name,
        status="active" if account.is_active else "suspended",
        registered_at=account.created_at,
        assignable_roles=assignable if role_name in PORTAL_ASSIGNABLE_STAFF_ROLES else [],
    )


def _serialize_by_key(session: Session, key: str) -> None:
    """Serialize concurrent writers of the same logical key on PostgreSQL (no-op elsewhere)."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
        )

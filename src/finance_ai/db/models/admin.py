from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base, BigIntPK
from finance_ai.db.models.identity import AppUser, Role


class StaffInvitation(Base):
    __tablename__ = "staff_invitation"
    __table_args__ = (
        CheckConstraint("email = lower(btrim(email))", name="email_normalized"),
        CheckConstraint("char_length(token_hash) = 64", name="token_hash_length"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumption_after_creation"
        ),
        CheckConstraint(
            "(consumed_at IS NULL AND activated_user_id IS NULL) OR "
            "(consumed_at IS NOT NULL AND activated_user_id IS NOT NULL)",
            name="consumption_matches_activation",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id", ondelete="RESTRICT"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_by_admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT")
    )

    role: Mapped[Role] = relationship(lazy="joined")
    creator: Mapped[AppUser] = relationship(foreign_keys=[created_by_admin_user_id], lazy="joined")
    activated_user: Mapped[AppUser | None] = relationship(
        foreign_keys=[activated_user_id], lazy="joined"
    )


class AdminTotpFactor(Base):
    __tablename__ = "admin_totp_factor"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'active', 'disabled')", name="approved_enrollment_state"
        ),
        CheckConstraint(
            "(state = 'active' AND activated_at IS NOT NULL) "
            "OR (state != 'active' AND activated_at IS NULL)",
            name="activation_timestamp_matches_state",
        ),
        CheckConstraint(
            "last_accepted_time_step IS NULL OR last_accepted_time_step >= 0",
            name="accepted_time_step_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_accepted_time_step: Mapped[int | None] = mapped_column(BigInteger)

    admin: Mapped[AppUser] = relationship(lazy="joined")


class StaffMfaChallenge(Base):
    """Short-lived, account-bound second-factor challenge with no privileged authority."""

    __tablename__ = "staff_mfa_challenge"
    __table_args__ = (
        CheckConstraint("char_length(token_hash) = 64", name="token_hash_length"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumption_after_creation"
        ),
        CheckConstraint("failed_attempts >= 0", name="failed_attempts_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )


class StaffMfaRecoveryCode(Base):
    """Hashed single-use recovery code for a lost authenticator (SEC-01)."""

    __tablename__ = "staff_mfa_recovery_code"
    __table_args__ = (
        CheckConstraint("char_length(code_hash) = 64", name="code_hash_length"),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumption_after_creation"
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountSuspension(Base):
    """History of administrator suspensions; at most one open row per account (ADM-02)."""

    __tablename__ = "account_suspension"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(reason)) > 0 AND char_length(reason) <= 500", name="reason_bounded"
        ),
        CheckConstraint(
            "(reactivated_at IS NULL AND reactivated_by_user_id IS NULL) OR "
            "(reactivated_at IS NOT NULL AND reactivated_by_user_id IS NOT NULL)",
            name="reactivation_pair",
        ),
        CheckConstraint(
            "reactivated_at IS NULL OR reactivated_at >= suspended_at",
            name="reactivation_after_suspension",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    suspended_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    suspended_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reactivated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    reactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "ix_staff_mfa_recovery_code_user_open",
    StaffMfaRecoveryCode.user_id,
    StaffMfaRecoveryCode.consumed_at,
)
Index("ix_staff_invitation_email_created", StaffInvitation.email, StaffInvitation.created_at)
Index(
    "ix_staff_mfa_challenge_user_created", StaffMfaChallenge.user_id, StaffMfaChallenge.created_at
)
Index("ix_staff_invitation_expiry", StaffInvitation.expires_at)
Index("ix_admin_totp_factor_user_state", AdminTotpFactor.admin_user_id, AdminTotpFactor.state)
Index(
    "uq_account_suspension_open",
    AccountSuspension.user_id,
    unique=True,
    postgresql_where=text("reactivated_at IS NULL"),
    sqlite_where=text("reactivated_at IS NULL"),
)
Index(
    "ix_account_suspension_user_history",
    AccountSuspension.user_id,
    AccountSuspension.suspended_at,
)

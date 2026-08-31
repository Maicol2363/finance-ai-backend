from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base
from finance_ai.db.models.identity import AppUser


class UserSession(Base):
    __tablename__ = "user_session"
    __table_args__ = (
        CheckConstraint("char_length(access_token_hash) = 64", name="access_hash_length"),
        CheckConstraint(
            "access_expires_at <= idle_expires_at AND idle_expires_at <= absolute_expires_at",
            name="session_expiry_order",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revocation_reason IS NULL) OR "
            "(revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)",
            name="revocation_pair",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(String(32))

    user: Mapped[AppUser] = relationship(lazy="joined")
    renewable_credentials: Mapped[list[RenewableCredential]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class RenewableCredential(Base):
    __tablename__ = "renewable_credential"
    __table_args__ = (
        CheckConstraint("char_length(token_hash) = 64", name="renewable_hash_length"),
        CheckConstraint("expires_at > issued_at", name="renewable_expiry_after_issue"),
        CheckConstraint(
            "replaced_by_id IS NULL OR consumed_at IS NOT NULL",
            name="replacement_requires_consumption",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("user_session.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("renewable_credential.id", ondelete="SET NULL")
    )

    session: Mapped[UserSession] = relationship(
        back_populates="renewable_credentials",
        foreign_keys=[session_id],
    )


Index(
    "ix_user_session_user_active_oldest",
    UserSession.user_id,
    UserSession.revoked_at,
    UserSession.created_at,
    UserSession.id,
)
Index(
    "ix_renewable_credential_session_history",
    RenewableCredential.session_id,
    RenewableCredential.issued_at,
    RenewableCredential.id,
)

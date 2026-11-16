from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_ai.db.base import Base, BigIntPK

NOTIFICATION_KINDS = ("budget_alert", "test")
OUTBOX_STATUSES = ("pending", "sending", "sent", "failed", "skipped")


class NotificationPreference(Base):
    """Per-user notification switches. Email is off until the person turns it on, and it can only
    be turned on once the address is verified (enforced by the service, not assumed here)."""

    __tablename__ = "notification_preference"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    email_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    budget_alerts: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    show_amounts: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EmailVerification(Base):
    """One open code per user: asking again replaces it. Only a hash of the code is stored."""

    __tablename__ = "email_verification"
    __table_args__ = (
        CheckConstraint("char_length(code_hash) = 64", name="code_hash_length"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumption_after_creation"
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


Index(
    "uq_email_verification_open",
    EmailVerification.user_id,
    unique=True,
    postgresql_where=text("consumed_at IS NULL"),
    sqlite_where=text("consumed_at IS NULL"),
)


class NotificationOutbox(Base):
    """A notification decided in the same transaction as the event that caused it.

    ``dedupe_key`` makes the decision idempotent. The payload never contains an email address or
    a secret: the dispatcher reads the current address from ``app_user`` when it sends. That is
    why the verification code is not queued here: it is sent directly in the request that asked
    for it, so it never sits in a table.
    """

    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint("kind IN ('budget_alert', 'test')", name="kind_approved"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'skipped')",
            name="status_approved",
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="sent_at_matches_status"),
        UniqueConstraint("dedupe_key"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


Index(
    "ix_notification_outbox_due",
    NotificationOutbox.next_attempt_at,
    postgresql_where=text("status IN ('pending', 'sending')"),
)
Index("ix_notification_outbox_user_sent", NotificationOutbox.user_id, NotificationOutbox.sent_at)

"""Email notifications: verified addresses, preferences, verification codes and the outbox.

Revision ID: 0021_email_notifications
Revises: 0020_admin_account_management
Create Date: 2026-11-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_email_notifications"
down_revision: str | Sequence[str] | None = "0020_admin_account_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing accounts start unverified; nothing is sent to them until they opt in and verify.
    op.add_column(
        "app_user", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "notification_preference",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("email_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("budget_alerts", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("show_amounts", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_notification_preference_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_notification_preference"),
    )

    op.create_table(
        "email_verification",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("char_length(code_hash) = 64", name="code_hash_length"),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumption_after_creation"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_email_verification_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_verification"),
    )
    op.create_index(
        "uq_email_verification_open",
        "email_verification",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("provider_message_id", sa.String(length=255)),
        sa.Column("last_error_code", sa.String(length=64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('budget_alert', 'test')", name="kind_approved"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'skipped')",
            name="status_approved",
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)", name="sent_at_matches_status"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_notification_outbox_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_outbox"),
        sa.UniqueConstraint("dedupe_key", name="uq_notification_outbox_dedupe_key"),
    )
    op.create_index(
        "ix_notification_outbox_due",
        "notification_outbox",
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('pending', 'sending')"),
    )
    op.create_index(
        "ix_notification_outbox_user_sent", "notification_outbox", ["user_id", "sent_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_user_sent", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_due", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_index("uq_email_verification_open", table_name="email_verification")
    op.drop_table("email_verification")
    op.drop_table("notification_preference")
    op.drop_column("app_user", "email_verified_at")

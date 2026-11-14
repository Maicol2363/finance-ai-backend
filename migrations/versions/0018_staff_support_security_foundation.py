"""Create the shared staff, support, and security data foundation.

Revision ID: 0018_staff_foundation
Revises: 0017_text_capture_idempotency
Create Date: 2026-11-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_staff_foundation"
down_revision: str | Sequence[str] | None = "0017_text_capture_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO role (name) VALUES ('support_agent'), ('security_auditor') "
        "ON CONFLICT (name) DO NOTHING"
    )

    op.add_column("user_session", sa.Column("mfa_verified_at", sa.DateTime(timezone=True)))

    op.create_table(
        "staff_invitation",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_admin_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("activated_user_id", sa.BigInteger()),
        sa.CheckConstraint("email = lower(btrim(email))", name="email_normalized"),
        sa.CheckConstraint("char_length(token_hash) = 64", name="token_hash_length"),
        sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="consumption_after_creation",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND activated_user_id IS NULL) OR "
            "(consumed_at IS NOT NULL AND activated_user_id IS NOT NULL)",
            name="consumption_matches_activation",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["role.id"], name="fk_staff_invitation_role_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_admin_user_id"],
            ["app_user.id"],
            name="fk_staff_invitation_created_by_admin_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["activated_user_id"],
            ["app_user.id"],
            name="fk_staff_invitation_activated_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staff_invitation"),
        sa.UniqueConstraint("token_hash", name="uq_staff_invitation_token_hash"),
    )
    op.create_index(
        "ix_staff_invitation_email_created", "staff_invitation", ["email", "created_at"]
    )
    op.create_index("ix_staff_invitation_expiry", "staff_invitation", ["expires_at"])

    op.create_table(
        "admin_totp_factor",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("admin_user_id", sa.BigInteger(), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "state", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("last_accepted_time_step", sa.BigInteger()),
        sa.CheckConstraint(
            "state IN ('pending', 'active', 'disabled')",
            name="approved_enrollment_state",
        ),
        sa.CheckConstraint(
            "(state = 'active' AND activated_at IS NOT NULL) "
            "OR (state != 'active' AND activated_at IS NULL)",
            name="activation_timestamp_matches_state",
        ),
        sa.CheckConstraint(
            "last_accepted_time_step IS NULL OR last_accepted_time_step >= 0",
            name="accepted_time_step_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["app_user.id"],
            name="fk_admin_totp_factor_admin_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_totp_factor"),
    )
    op.create_index(
        "ix_admin_totp_factor_user_state", "admin_totp_factor", ["admin_user_id", "state"]
    )

    op.create_table(
        "security_audit_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("actor_id", sa.String(length=128)),
        sa.Column("target_resource_type", sa.String(length=100)),
        sa.Column("target_resource_id", sa.String(length=128)),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("correlation_id", sa.String(length=128)),
        sa.Column(
            "context",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_security_audit_event"),
    )
    op.create_index(
        "ix_security_audit_event_chronology",
        "security_audit_event",
        [sa.text("occurred_at DESC"), sa.text("event_id DESC")],
    )
    op.create_index(
        "ix_security_audit_event_actor_chronology",
        "security_audit_event",
        ["actor_id", sa.text("occurred_at DESC"), sa.text("event_id DESC")],
    )
    op.create_index(
        "ix_security_audit_event_target_chronology",
        "security_audit_event",
        [
            "target_resource_type",
            "target_resource_id",
            sa.text("occurred_at DESC"),
            sa.text("event_id DESC"),
        ],
    )
    op.create_index(
        "ix_security_audit_event_type_chronology",
        "security_audit_event",
        ["event_type", sa.text("occurred_at DESC"), sa.text("event_id DESC")],
    )
    op.create_index(
        "ix_security_audit_event_result_chronology",
        "security_audit_event",
        ["result", sa.text("occurred_at DESC"), sa.text("event_id DESC")],
    )
    op.execute(
        "CREATE FUNCTION prevent_security_audit_event_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'security audit events are append-only'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER security_audit_event_append_only "
        "BEFORE UPDATE OR DELETE ON security_audit_event "
        "FOR EACH ROW EXECUTE FUNCTION prevent_security_audit_event_mutation()"
    )

    op.create_table(
        "support_ticket",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("customer_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("assigned_staff_user_id", sa.BigInteger()),
        sa.Column("originating_support_agent_id", sa.BigInteger()),
        sa.Column("resolution_summary", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'escalated', 'resolved')",
            name="approved_status",
        ),
        sa.CheckConstraint("length(trim(subject)) > 0", name="subject_nonempty"),
        sa.CheckConstraint("length(trim(category)) > 0", name="category_nonempty"),
        sa.CheckConstraint("length(trim(description)) > 0", name="description_nonempty"),
        sa.ForeignKeyConstraint(
            ["customer_user_id"],
            ["app_user.id"],
            name="fk_support_ticket_customer_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            name="fk_support_ticket_created_by_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_staff_user_id"],
            ["app_user.id"],
            name="fk_support_ticket_assigned_staff_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["originating_support_agent_id"],
            ["app_user.id"],
            name="fk_support_ticket_originating_support_agent_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_support_ticket"),
    )
    op.create_index("ix_support_ticket_status_created", "support_ticket", ["status", "created_at"])
    op.create_index(
        "ix_support_ticket_assignee_status", "support_ticket", ["assigned_staff_user_id", "status"]
    )
    op.create_index(
        "ix_support_ticket_customer_created",
        "support_ticket",
        ["customer_user_id", "created_at", "id"],
    )

    op.create_table(
        "support_ticket_message",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column("author_user_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(body)) > 0", name="body_nonempty"),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["support_ticket.id"],
            name="fk_support_ticket_message_ticket_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["app_user.id"],
            name="fk_support_ticket_message_author_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_support_ticket_message"),
    )
    op.create_index(
        "ix_support_ticket_message_history",
        "support_ticket_message",
        ["ticket_id", "created_at", "id"],
    )

    op.create_table(
        "support_ticket_activity",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("previous_status", sa.String(length=20)),
        sa.Column("new_status", sa.String(length=20)),
        sa.Column("previous_assignee_user_id", sa.BigInteger()),
        sa.Column("new_assignee_user_id", sa.BigInteger()),
        sa.Column("internal_reason", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "previous_status IS NULL OR previous_status IN "
            "('open', 'in_progress', 'escalated', 'resolved')",
            name="previous_status_approved",
        ),
        sa.CheckConstraint(
            "new_status IS NULL OR new_status IN ('open', 'in_progress', 'escalated', 'resolved')",
            name="new_status_approved",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["support_ticket.id"],
            name="fk_support_ticket_activity_ticket_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_user.id"],
            name="fk_support_ticket_activity_actor_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_assignee_user_id"],
            ["app_user.id"],
            name="fk_support_ticket_activity_previous_assignee_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["new_assignee_user_id"],
            ["app_user.id"],
            name="fk_support_ticket_activity_new_assignee_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_support_ticket_activity"),
    )
    op.create_index(
        "ix_support_ticket_activity_history",
        "support_ticket_activity",
        ["ticket_id", "created_at", "id"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        persisted_events = op.get_bind().scalar(
            sa.text("SELECT count(*) FROM security_audit_event")
        )
        if persisted_events:
            raise RuntimeError(
                "Refusing to drop security_audit_event while it contains audit evidence"
            )
    op.execute("DROP TRIGGER IF EXISTS security_audit_event_append_only ON security_audit_event")
    op.execute("DROP FUNCTION IF EXISTS prevent_security_audit_event_mutation()")
    op.drop_table("support_ticket_activity")
    op.drop_table("support_ticket_message")
    op.drop_index("ix_support_ticket_customer_created", table_name="support_ticket")
    op.drop_index("ix_support_ticket_assignee_status", table_name="support_ticket")
    op.drop_index("ix_support_ticket_status_created", table_name="support_ticket")
    op.drop_table("support_ticket")
    op.drop_index("ix_security_audit_event_result_chronology", table_name="security_audit_event")
    op.drop_index("ix_security_audit_event_type_chronology", table_name="security_audit_event")
    op.drop_index("ix_security_audit_event_target_chronology", table_name="security_audit_event")
    op.drop_index("ix_security_audit_event_actor_chronology", table_name="security_audit_event")
    op.drop_index("ix_security_audit_event_chronology", table_name="security_audit_event")
    op.drop_table("security_audit_event")
    op.drop_index("ix_admin_totp_factor_user_state", table_name="admin_totp_factor")
    op.drop_table("admin_totp_factor")
    op.drop_index("ix_staff_invitation_expiry", table_name="staff_invitation")
    op.drop_index("ix_staff_invitation_email_created", table_name="staff_invitation")
    op.drop_table("staff_invitation")
    op.drop_column("user_session", "mfa_verified_at")
    # Keep role rows: they may have become referenced after this revision.

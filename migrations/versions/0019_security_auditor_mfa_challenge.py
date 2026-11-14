"""Add short-lived staff MFA challenges for the Security Auditor sign-in.

Revision ID: 0019_staff_mfa_challenge
Revises: 0018_staff_foundation
Create Date: 2026-11-14 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_staff_mfa_challenge"
down_revision: str | Sequence[str] | None = "0018_staff_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "staff_mfa_challenge",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("char_length(token_hash) = 64", name="token_hash_length"),
        sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="consumption_after_creation",
        ),
        sa.CheckConstraint("failed_attempts >= 0", name="failed_attempts_nonnegative"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_staff_mfa_challenge_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staff_mfa_challenge"),
        sa.UniqueConstraint("token_hash", name="uq_staff_mfa_challenge_token_hash"),
    )
    op.create_index(
        "ix_staff_mfa_challenge_user_created",
        "staff_mfa_challenge",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_staff_mfa_challenge_user_created", table_name="staff_mfa_challenge")
    op.drop_table("staff_mfa_challenge")

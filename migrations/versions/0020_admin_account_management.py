"""Administrator role: MFA recovery, session role binding and account management.

Revision ID: 0020_admin_account_management
Revises: 0019_staff_mfa_challenge
Create Date: 2026-11-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_admin_account_management"
down_revision: str | Sequence[str] | None = "0019_staff_mfa_challenge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A staff session remembers the role it was issued for, so a role change can never carry
    # an old (possibly MFA-verified) session over to different authority (SEC-01, ADM-04).
    op.add_column("user_session", sa.Column("issued_role_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_user_session_issued_role_id_role",
        "user_session",
        "role",
        ["issued_role_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "staff_mfa_recovery_code",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("char_length(code_hash) = 64", name="code_hash_length"),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumption_after_creation"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_staff_mfa_recovery_code_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staff_mfa_recovery_code"),
        sa.UniqueConstraint("code_hash", name="uq_staff_mfa_recovery_code_code_hash"),
    )
    op.create_index(
        "ix_staff_mfa_recovery_code_user_open",
        "staff_mfa_recovery_code",
        ["user_id", "consumed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_staff_mfa_recovery_code_user_open", table_name="staff_mfa_recovery_code")
    op.drop_table("staff_mfa_recovery_code")
    op.drop_constraint("fk_user_session_issued_role_id_role", "user_session", type_="foreignkey")
    op.drop_column("user_session", "issued_role_id")

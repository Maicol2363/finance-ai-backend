"""Create UOW-02 revocable user sessions and renewable credential history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_uow02"
down_revision: str | Sequence[str] | None = "0001_uow01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_session",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("access_token_hash", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "char_length(access_token_hash) = 64",
            name="ck_user_session_access_hash_length",
        ),
        sa.CheckConstraint(
            "access_expires_at <= idle_expires_at AND idle_expires_at <= absolute_expires_at",
            name="ck_user_session_session_expiry_order",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revocation_reason IS NULL) OR "
            "(revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)",
            name="ck_user_session_revocation_pair",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_user_session_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_session"),
        sa.UniqueConstraint("access_token_hash", name="uq_user_session_access_token_hash"),
    )
    op.create_index(
        "ix_user_session_user_active_oldest",
        "user_session",
        ["user_id", "revoked_at", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "renewable_credential",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "char_length(token_hash) = 64",
            name="ck_renewable_credential_renewable_hash_length",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_renewable_credential_renewable_expiry_after_issue",
        ),
        sa.CheckConstraint(
            "replaced_by_id IS NULL OR consumed_at IS NOT NULL",
            name="ck_renewable_credential_replacement_requires_consumption",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["renewable_credential.id"],
            name="fk_renewable_credential_replaced_by_id_renewable_credential",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["user_session.id"],
            name="fk_renewable_credential_session_id_user_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_renewable_credential"),
        sa.UniqueConstraint("token_hash", name="uq_renewable_credential_token_hash"),
    )
    op.create_index(
        "ix_renewable_credential_session_history",
        "renewable_credential",
        ["session_id", "issued_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_renewable_credential_session_history", table_name="renewable_credential")
    op.drop_table("renewable_credential")
    op.drop_index("ix_user_session_user_active_oldest", table_name="user_session")
    op.drop_table("user_session")

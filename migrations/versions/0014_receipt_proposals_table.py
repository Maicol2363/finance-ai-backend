"""Create receipt_proposals table for temporary draft storage.

Revision ID: 0014_receipt_proposals_table
Revises: 0013_align_indexes_with_models
Create Date: 2026-09-27 15:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_receipt_proposals_table"
down_revision: str | Sequence[str] | None = "0013_align_indexes_with_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_col = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    op.create_table(
        "receipt_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="partial", nullable=False),
        sa.Column("proposal_data", json_col, nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_receipt_proposals_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_receipt_proposals"),
    )
    op.create_index(
        "ix_receipt_proposals_user_id",
        "receipt_proposals",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_receipt_proposals_user_id", table_name="receipt_proposals")
    op.drop_table("receipt_proposals")

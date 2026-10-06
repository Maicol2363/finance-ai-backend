"""Create text_capture_proposals for HU-013 draft storage.

Revision ID: 0016_hu013_text_capture
Revises: 0015_merge_hu015_budgets
Create Date: 2026-10-02 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_hu013_text_capture"
down_revision: str | Sequence[str] | None = "0015_merge_hu015_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_col = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    op.create_table(
        "text_capture_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("proposal_data", json_col, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
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
            name="fk_text_capture_proposals_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_text_capture_proposals"),
    )
    op.create_index(
        "ix_text_capture_proposals_user_id",
        "text_capture_proposals",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_text_capture_proposals_user_id", table_name="text_capture_proposals")
    op.drop_table("text_capture_proposals")

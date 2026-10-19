"""Persist text proposal IDs on financial operations to make retries idempotent.

Revision ID: 0017_text_capture_idempotency
Revises: 0016_hu013_text_capture
Create Date: 2026-10-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_text_capture_idempotency"
down_revision: str | Sequence[str] | None = "0016_hu013_text_capture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "financial_transaction",
        sa.Column("source_proposal_id", sa.Uuid(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_financial_transaction_source_proposal",
        "financial_transaction",
        ["user_id", "source_proposal_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_financial_transaction_source_proposal",
        "financial_transaction",
        type_="unique",
    )
    op.drop_column("financial_transaction", "source_proposal_id")

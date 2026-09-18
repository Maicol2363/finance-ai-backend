"""Add timestamps required by the expense API and repair the HU-008 index.

Revision ID: 0010_expense_audit_index
Revises: 0009_hu008_hu011_expense_history

The index is declared by revision 0009.  Some local databases reached that
revision before the index was added to the migration, so this revision uses
``IF NOT EXISTS`` to make the repair safe for both database shapes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_expense_audit_index"
down_revision: str | Sequence[str] | None = "0009_hu008_hu011_expense_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "receipt",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "receipt",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "expense",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "expense",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_receipt_category_date ON receipt (category_id, date)"
    )


def downgrade() -> None:
    op.drop_column("expense", "updated_at")
    op.drop_column("expense", "created_at")
    op.drop_column("receipt", "updated_at")
    op.drop_column("receipt", "created_at")

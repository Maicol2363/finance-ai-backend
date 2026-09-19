"""Add the income-source lookup index used by HU-009.

Revision ID: 0011_hu009_income_source_index
Revises: 0010_expense_audit_index
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_hu009_income_source_index"
down_revision: str | Sequence[str] | None = "0010_expense_audit_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_financial_transaction_income_source",
        "financial_transaction",
        ["income_source_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_financial_transaction_income_source", table_name="financial_transaction")

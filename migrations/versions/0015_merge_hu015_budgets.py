"""Merge the budgets and receipt proposal branches.

Revision ID: 0015_merge_hu015_budgets
Revises: 0014_hg016_budgets, 0014_receipt_proposals_table
Create Date: 2026-09-28 17:04:04.214610
"""

from collections.abc import Sequence

revision: str = "0015_merge_hu015_budgets"
down_revision: str | Sequence[str] | None = ("0014_hg016_budgets", "0014_receipt_proposals_table")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

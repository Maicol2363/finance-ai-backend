"""Make income_source names unique per user without archive filtering.

Revision ID: 0007_uow03_income_source
Revises: 0006_category_management
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_uow03_income_source"
down_revision: str | Sequence[str] | None = "0006_category_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_income_source_active_name", table_name="income_source")
    op.create_index(
        "uq_income_source_user_name",
        "income_source",
        ["user_id", sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_income_source_user_name", table_name="income_source")
    op.create_index(
        "uq_income_source_active_name",
        "income_source",
        ["user_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

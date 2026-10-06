"""Enforce owned category budgets and add progress-query indexes.

Revision ID: 0014_hg016_budgets
Revises: 0013_align_indexes_with_models
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_hg016_budgets"
down_revision: str | Sequence[str] | None = "0013_align_indexes_with_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A category budget must have the same owner as its category. Remove any
    # legacy cross-owner rows before installing the database-level invariant.
    op.execute(
        """
        DELETE FROM budget AS budget_row
        USING category AS category_row
        WHERE budget_row.category_id = category_row.id
          AND budget_row.user_id <> category_row.user_id
        """
    )
    op.drop_constraint("fk_budget_category_id_category", "budget", type_="foreignkey")
    op.create_unique_constraint(
        "uq_category_id_user_id",
        "category",
        ["id", "user_id"],
    )
    op.create_foreign_key(
        "fk_budget_category_owner",
        "budget",
        "category",
        ["category_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "budget_schedule_type_supported",
        "budget",
        "schedule_type IN ('recurring', 'one_time')",
    )
    op.create_check_constraint(
        "budget_frequency_unit_supported",
        "budget",
        "frequency_unit IS NULL OR frequency_unit IN ('day', 'week', 'month', 'year')",
    )
    op.create_index(
        "ix_expense_budget_progress",
        "expense",
        ["user_id", "category_id", "receipt_id"],
    )
    op.create_index(
        "ix_receipt_budget_progress",
        "receipt",
        ["user_id", "currency_id", "date", "transaction_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_receipt_budget_progress", table_name="receipt")
    op.drop_index("ix_expense_budget_progress", table_name="expense")
    op.drop_constraint(
        op.f("ck_budget_budget_frequency_unit_supported"),
        "budget",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_budget_budget_schedule_type_supported"),
        "budget",
        type_="check",
    )
    op.drop_constraint("fk_budget_category_owner", "budget", type_="foreignkey")
    op.drop_constraint("uq_category_id_user_id", "category", type_="unique")
    op.create_foreign_key(
        "fk_budget_category_id_category",
        "budget",
        "category",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )

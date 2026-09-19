"""Add store table, budget table, and expand transaction capture_source.

Revision ID: 0012_dbml_store_budget
Revises: 0011_hu009_income_source_index
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_dbml_store_budget"
down_revision: str | Sequence[str] | None = "0011_hu009_income_source_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create store table
    op.create_table(
        "store",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("normalized_name", sa.String(length=150), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_store_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_store"),
        sa.UniqueConstraint("user_id", "normalized_name", name="uq_store_user_normalized_name"),
    )

    # 2. Add store_id to receipt
    op.add_column("receipt", sa.Column("store_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_receipt_store_id_store",
        "receipt",
        "store",
        ["store_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_receipt_store_date", "receipt", ["store_id", "date"])

    # 3. Expand capture_source on financial_transaction
    op.alter_column(
        "financial_transaction",
        "capture_source",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.drop_constraint("capture_source_supported", "financial_transaction", type_="check")
    op.create_check_constraint(
        "capture_source_supported",
        "financial_transaction",
        "capture_source IN ("
        "'manual', 'ai', 'text_ai', 'audio_ai', 'receipt_photo_ai', 'import'"
        ")",
    )

    # 4. Create budget table
    op.create_table(
        "budget",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("schedule_type", sa.String(length=20), nullable=False),
        sa.Column("frequency_unit", sa.String(length=10), nullable=True),
        sa.Column("frequency_interval", sa.SmallInteger(), nullable=True),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="budget_amount_positive"),
        sa.CheckConstraint("ends_on IS NULL OR ends_on >= starts_on", name="budget_date_order"),
        sa.CheckConstraint(
            "frequency_interval IS NULL OR frequency_interval > 0",
            name="budget_interval_positive",
        ),
        sa.CheckConstraint(
            "("
            "(schedule_type = 'recurring' AND frequency_unit IS NOT NULL "
            "AND frequency_interval IS NOT NULL) "
            "OR (schedule_type = 'one_time' AND frequency_unit IS NULL "
            "AND frequency_interval IS NULL)"
            ")",
            name="budget_schedule_fields",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_budget_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["currency_id"],
            ["currency.id"],
            name="fk_budget_currency_id_currency",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
            name="fk_budget_category_id_category",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_budget"),
    )
    op.create_index("ix_budget_user_active", "budget", ["user_id", "is_active"])
    op.create_index("ix_budget_user_dates", "budget", ["user_id", "starts_on", "ends_on"])
    op.create_index("ix_budget_category_active", "budget", ["category_id", "is_active"])


def downgrade() -> None:
    op.drop_index("ix_budget_category_active", table_name="budget")
    op.drop_index("ix_budget_user_dates", table_name="budget")
    op.drop_index("ix_budget_user_active", table_name="budget")
    op.drop_table("budget")

    op.drop_constraint("capture_source_supported", "financial_transaction", type_="check")
    op.create_check_constraint(
        "capture_source_supported",
        "financial_transaction",
        "capture_source IN ('manual', 'ai', 'import')",
    )
    op.alter_column(
        "financial_transaction",
        "capture_source",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )

    op.drop_index("idx_receipt_store_date", table_name="receipt")
    op.drop_constraint("fk_receipt_store_id_store", "receipt", type_="foreignkey")
    op.drop_column("receipt", "store_id")

    op.drop_table("store")

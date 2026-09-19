"""Connect expense records to financial history and add editable detail fields.

Revision ID: 0009_hu008_hu011_expense_history
Revises: 0008_complete_dbml_schema
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_hu008_hu011_expense_history"
down_revision: str | Sequence[str] | None = "0008_complete_dbml_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "financial_transaction",
        sa.Column("income_source_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_financial_transaction_income_source_id_income_source",
        "financial_transaction",
        "income_source",
        ["income_source_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "income_source_only_for_income",
        "financial_transaction",
        "type = 'income' OR income_source_id IS NULL",
    )
    op.create_index(
        "ix_financial_transaction_user_occurred",
        "financial_transaction",
        ["user_id", sa.text("occurred_at DESC"), sa.text("id DESC")],
    )

    op.add_column(
        "receipt",
        sa.Column("transaction_id", sa.BigInteger(), nullable=True),
    )
    op.add_column("receipt", sa.Column("category_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_receipt_transaction_id_financial_transaction",
        "receipt",
        "financial_transaction",
        ["transaction_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_receipt_category_id_category",
        "receipt",
        "category",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("uq_receipt_transaction_id", "receipt", ["transaction_id"], unique=True)
    op.create_index("idx_receipt_category_date", "receipt", ["category_id", "date"])

    op.add_column("expense", sa.Column("category_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "expense",
        sa.Column("position", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "expense",
        sa.Column(
            "quantity",
            sa.Numeric(precision=12, scale=3),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column("expense", sa.Column("unit_price", sa.Numeric(precision=12, scale=2)))
    op.add_column("expense", sa.Column("note", sa.Text()))
    op.add_column("expense", sa.Column("image_path", sa.Text()))
    op.alter_column(
        "expense",
        "subcategory_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_expense_category_id_category",
        "expense",
        "category",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint("expense_position_positive", "expense", "position > 0")
    op.create_check_constraint("expense_quantity_positive", "expense", "quantity > 0")
    op.create_check_constraint(
        "expense_unit_price_non_negative",
        "expense",
        "unit_price IS NULL OR unit_price >= 0",
    )
    op.create_unique_constraint(
        "uq_expense_receipt_position", "expense", ["receipt_id", "position"]
    )
    op.create_index("ix_expense_category", "expense", ["category_id"])


def downgrade() -> None:
    op.drop_index("ix_expense_category", table_name="expense")
    op.drop_constraint("uq_expense_receipt_position", "expense", type_="unique")
    op.drop_constraint("expense_unit_price_non_negative", "expense", type_="check")
    op.drop_constraint("expense_quantity_positive", "expense", type_="check")
    op.drop_constraint("expense_position_positive", "expense", type_="check")
    op.drop_constraint("fk_expense_category_id_category", "expense", type_="foreignkey")
    # Revision 0008 required a subcategory.  An uncategorized item cannot be
    # represented after downgrade, so remove only those incompatible rows
    # before restoring the old NOT NULL constraint.
    op.execute("DELETE FROM expense WHERE subcategory_id IS NULL")
    op.alter_column(
        "expense",
        "subcategory_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.drop_column("expense", "image_path")
    op.drop_column("expense", "note")
    op.drop_column("expense", "unit_price")
    op.drop_column("expense", "quantity")
    op.drop_column("expense", "position")
    op.drop_column("expense", "category_id")

    op.drop_index("idx_receipt_category_date", table_name="receipt")
    op.drop_index("uq_receipt_transaction_id", table_name="receipt")
    op.drop_constraint("fk_receipt_category_id_category", "receipt", type_="foreignkey")
    op.drop_constraint(
        "fk_receipt_transaction_id_financial_transaction", "receipt", type_="foreignkey"
    )
    op.drop_column("receipt", "category_id")
    op.drop_column("receipt", "transaction_id")

    op.drop_index("ix_financial_transaction_user_occurred", table_name="financial_transaction")
    op.drop_constraint("income_source_only_for_income", "financial_transaction", type_="check")
    op.drop_constraint(
        "fk_financial_transaction_income_source_id_income_source",
        "financial_transaction",
        type_="foreignkey",
    )
    op.drop_column("financial_transaction", "income_source_id")

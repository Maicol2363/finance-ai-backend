"""Complete DBML schema tables, columns, constraints, and indexes.

Revision ID: 0008_complete_dbml_schema
Revises: 0007_uow03_income_source
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_complete_dbml_schema"
down_revision: str | Sequence[str] | None = "0007_uow03_income_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Role table & user.role_id column
    op.create_table(
        "role",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_role"),
        sa.UniqueConstraint("name", name="uq_role_name"),
    )
    op.execute("INSERT INTO role (id, name) VALUES (1, 'user') ON CONFLICT DO NOTHING")

    op.add_column(
        "app_user",
        sa.Column(
            "role_id",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_app_user_role_id_role",
        "app_user",
        "role",
        ["role_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 2. Subcategory table
    op.create_table(
        "subcategory",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
            name="fk_subcategory_category_id_category",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subcategory"),
    )

    # 3. PaymentMethod table
    op.create_table(
        "paymentmethod",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_paymentmethod"),
        sa.UniqueConstraint("name", name="uq_paymentmethod_name"),
    )

    # 4. Receipt table
    op.create_table(
        "receipt",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("store", sa.String(length=150), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column("purchase_reason", sa.Text(), nullable=True),
        sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("payment_method_id", sa.BigInteger(), nullable=False),
        sa.Column("image", sa.Text(), nullable=True),
        sa.CheckConstraint("total > 0", name="receipt_total_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_receipt_user_id_app_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["currency_id"],
            ["currency.id"],
            name="fk_receipt_currency_id_currency",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_method_id"],
            ["paymentmethod.id"],
            name="fk_receipt_payment_method_id_paymentmethod",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_receipt"),
    )
    op.create_index("idx_receipt_user_id", "receipt", ["user_id"])
    op.create_index("idx_receipt_date", "receipt", ["date"])
    op.create_index("idx_receipt_payment", "receipt", ["payment_method_id"])

    # 5. Expense table
    op.create_table(
        "expense",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column("subcategory_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("item_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("amount > 0", name="expense_amount_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_expense_user_id_app_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["receipt.id"],
            name="fk_expense_receipt_id_receipt",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subcategory_id"],
            ["subcategory.id"],
            name="fk_expense_subcategory_id_subcategory",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_expense"),
    )
    op.create_index("idx_expense_user_id", "expense", ["user_id"])
    op.create_index("idx_expense_receipt", "expense", ["receipt_id"])
    op.create_index("idx_expense_subcat", "expense", ["subcategory_id"])

    # 6. SpendingLimit table
    op.create_table(
        "spendinglimit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column("subcategory_id", sa.BigInteger(), nullable=True),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("amount > 0", name="spendinglimit_amount_positive"),
        sa.CheckConstraint("period IN ('weekly', 'monthly')", name="spendinglimit_period_valid"),
        sa.CheckConstraint(
            "(subcategory_id IS NOT NULL AND category_id IS NULL) "
            "OR (subcategory_id IS NULL AND category_id IS NOT NULL)",
            name="spendinglimit_xor_target",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_spendinglimit_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subcategory_id"],
            ["subcategory.id"],
            name="fk_spendinglimit_subcategory_id_subcategory",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
            name="fk_spendinglimit_category_id_category",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_spendinglimit"),
    )
    op.create_index("idx_limit_user_id", "spendinglimit", ["user_id"])

    # 7. Incomesource table
    op.create_table(
        "incomesource",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_incomesource"),
        sa.UniqueConstraint("name", name="uq_incomesource_name"),
    )

    # 8. Income table
    op.create_table(
        "income",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("income_source_id", sa.BigInteger(), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.CheckConstraint("amount > 0", name="income_amount_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_income_user_id_app_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["income_source_id"],
            ["incomesource.id"],
            name="fk_income_income_source_id_incomesource",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["currency_id"],
            ["currency.id"],
            name="fk_income_currency_id_currency",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_income"),
    )
    op.create_index("idx_income_user_id", "income", ["user_id"])
    op.create_index("idx_income_date", "income", ["date"])
    op.create_index("idx_income_source", "income", ["income_source_id"])


def downgrade() -> None:
    op.drop_index("idx_income_source", table_name="income")
    op.drop_index("idx_income_date", table_name="income")
    op.drop_index("idx_income_user_id", table_name="income")
    op.drop_table("income")

    op.drop_table("incomesource")

    op.drop_index("idx_limit_user_id", table_name="spendinglimit")
    op.drop_table("spendinglimit")

    op.drop_index("idx_expense_subcat", table_name="expense")
    op.drop_index("idx_expense_receipt", table_name="expense")
    op.drop_index("idx_expense_user_id", table_name="expense")
    op.drop_table("expense")

    op.drop_index("idx_receipt_payment", table_name="receipt")
    op.drop_index("idx_receipt_date", table_name="receipt")
    op.drop_index("idx_receipt_user_id", table_name="receipt")
    op.drop_table("receipt")

    op.drop_table("paymentmethod")

    op.drop_table("subcategory")

    op.drop_constraint("fk_app_user_role_id_role", "app_user", type_="foreignkey")
    op.drop_column("app_user", "role_id")
    op.drop_table("role")

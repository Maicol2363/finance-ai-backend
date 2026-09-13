"""Create UOW-03 financial accounts and internal movement foundations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_uow03_financial_accounts"
down_revision: str | Sequence[str] | None = "0003_uow02_constraint_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("currency_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "opening_balance",
            sa.Numeric(precision=18, scale=2),
            server_default=sa.text("0.00"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 100",
            name="name_trimmed_non_empty",
        ),
        sa.CheckConstraint(
            "type IN ('cash', 'bank', 'digital_wallet', 'savings', 'credit_card', 'other')",
            name="type_supported",
        ),
        sa.ForeignKeyConstraint(
            ["currency_id"],
            ["currency.id"],
            name="fk_account_currency_id_currency",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_account_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account"),
        sa.UniqueConstraint("id", "user_id", name="uq_account_id_user_id"),
    )
    op.create_index(
        "uq_account_active_name",
        "account",
        ["user_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_account_user_active_name",
        "account",
        ["user_id", "is_active", sa.text("lower(name)"), "id"],
        unique=False,
    )
    op.create_index(
        "ix_account_user_created",
        "account",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )

    op.create_table(
        "financial_transaction",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("capture_source", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('expense', 'income', 'transfer', 'adjustment')",
            name="type_supported",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'voided')",
            name="status_supported",
        ),
        sa.CheckConstraint(
            "capture_source IN ('manual', 'ai', 'import')",
            name="capture_source_supported",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_financial_transaction_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_financial_transaction"),
        sa.UniqueConstraint("id", "user_id", name="uq_financial_transaction_id_user_id"),
    )
    op.create_index(
        "ix_financial_transaction_user_status",
        "financial_transaction",
        ["user_id", "status", "id"],
        unique=False,
    )

    op.create_table(
        "account_movement",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("transaction_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("amount <> 0", name="amount_non_zero"),
        sa.ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["account.id", "account.user_id"],
            name="fk_account_movement_account_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id", "user_id"],
            ["financial_transaction.id", "financial_transaction.user_id"],
            name="fk_account_movement_transaction_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account_movement"),
    )
    op.create_index(
        "ix_account_movement_account_transaction",
        "account_movement",
        ["account_id", "transaction_id"],
        unique=False,
    )
    op.create_index(
        "ix_account_movement_user_account",
        "account_movement",
        ["user_id", "account_id", "transaction_id"],
        unique=False,
    )
    op.create_index(
        "ix_account_movement_transaction",
        "account_movement",
        ["transaction_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_account_movement_transaction", table_name="account_movement")
    op.drop_index("ix_account_movement_user_account", table_name="account_movement")
    op.drop_index("ix_account_movement_account_transaction", table_name="account_movement")
    op.drop_table("account_movement")
    op.drop_index("ix_financial_transaction_user_status", table_name="financial_transaction")
    op.drop_table("financial_transaction")
    op.drop_index("ix_account_user_created", table_name="account")
    op.drop_index("ix_account_user_active_name", table_name="account")
    op.drop_index("uq_account_active_name", table_name="account")
    op.drop_table("account")

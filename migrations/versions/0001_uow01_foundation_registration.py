"""Create UOW-01 identity, currency, and starter-catalog tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_uow01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "currency",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("code", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("decimals", sa.SmallInteger(), server_default=sa.text("2"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint("decimals >= 0", name="decimals_non_negative"),
        sa.PrimaryKeyConstraint("id", name="pk_currency"),
        sa.UniqueConstraint("code", name="uq_currency_code"),
    )
    op.create_table(
        "app_user",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("preferred_currency_id", sa.BigInteger(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["preferred_currency_id"],
            ["currency.id"],
            name="fk_app_user_preferred_currency_id_currency",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "email = lower(btrim(email))",
            name="email_normalized",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_app_user"),
    )
    op.create_index(
        "uq_app_user_email",
        "app_user",
        [sa.text("lower(email)")],
        unique=True,
    )
    _create_owned_catalog("category")
    _create_owned_catalog("payment_method")
    _create_owned_catalog("income_source")


def _create_owned_catalog(table_name: str) -> None:
    op.create_table(
        table_name,
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=f"fk_{table_name}_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table_name}"),
    )
    op.create_index(
        f"uq_{table_name}_active_name",
        table_name,
        ["user_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )


def downgrade() -> None:
    op.drop_index("uq_income_source_active_name", table_name="income_source")
    op.drop_table("income_source")
    op.drop_index("uq_payment_method_active_name", table_name="payment_method")
    op.drop_table("payment_method")
    op.drop_index("uq_category_active_name", table_name="category")
    op.drop_table("category")
    op.drop_index("uq_app_user_email", table_name="app_user")
    op.drop_table("app_user")
    op.drop_table("currency")

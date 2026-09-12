"""Remove the unused payment-method catalog.

Accounts are the sole representation of where money is held or spent from.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_drop_payment_method"
down_revision: str | Sequence[str] | None = "0004_uow03_financial_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CASCADE also removes optional read-only views created by the DBeaver helper.
    # Re-running the current helper recreates only the supported views.
    op.execute("DROP TABLE payment_method CASCADE")


def downgrade() -> None:
    op.create_table(
        "payment_method",
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
            name="fk_payment_method_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_method"),
    )
    op.create_index(
        "uq_payment_method_active_name",
        "payment_method",
        ["user_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

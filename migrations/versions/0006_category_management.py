"""Add owned category management fields and permanent naming semantics."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_category_management"
down_revision: str | Sequence[str] | None = "0005_drop_payment_method"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLORS = "'blue', 'green', 'orange', 'purple', 'pink', 'red', 'teal', 'yellow', 'gray'"
ICONS = (
    "'essentials', 'wants', 'food', 'transport', 'home', 'health', 'education', "
    "'entertainment', 'shopping', 'bills', 'travel', 'gift', 'other'"
)


def upgrade() -> None:
    op.add_column(
        "category",
        sa.Column("color", sa.String(length=20), server_default="blue", nullable=False),
    )
    op.add_column(
        "category",
        sa.Column("icon", sa.String(length=30), server_default="other", nullable=False),
    )
    op.add_column(
        "category",
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY user_id ORDER BY id) - 1 AS position
            FROM category
        )
        UPDATE category
        SET sort_order = ranked.position,
            color = CASE lower(category.name)
                WHEN 'needs' THEN 'blue' WHEN 'wants' THEN 'purple' ELSE 'gray' END,
            icon = CASE lower(category.name)
                WHEN 'needs' THEN 'essentials' WHEN 'wants' THEN 'wants' ELSE 'other' END
        FROM ranked
        WHERE category.id = ranked.id
        """
    )
    op.drop_index("uq_category_active_name", table_name="category")
    _drop_optional_readonly_view()
    op.drop_column("category", "is_active")
    op.create_index(
        "uq_category_user_name",
        "category",
        ["user_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index(
        "ix_category_user_sort", "category", ["user_id", "sort_order", "id"], unique=False
    )
    op.create_check_constraint(
        "name_trimmed",
        "category",
        "name = btrim(name) AND char_length(name) > 0",
    )
    op.create_check_constraint("color_supported", "category", f"color IN ({COLORS})")
    op.create_check_constraint("icon_supported", "category", f"icon IN ({ICONS})")
    op.create_check_constraint("sort_order_non_negative", "category", "sort_order >= 0")
    _create_optional_readonly_view(include_active=False)


def downgrade() -> None:
    _drop_optional_readonly_view()
    op.drop_constraint("ck_category_sort_order_non_negative", "category", type_="check")
    op.drop_constraint("ck_category_icon_supported", "category", type_="check")
    op.drop_constraint("ck_category_color_supported", "category", type_="check")
    op.drop_constraint("ck_category_name_trimmed", "category", type_="check")
    op.drop_index("ix_category_user_sort", table_name="category")
    op.drop_index("uq_category_user_name", table_name="category")
    op.add_column(
        "category",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.create_index(
        "uq_category_active_name",
        "category",
        ["user_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.drop_column("category", "sort_order")
    op.drop_column("category", "icon")
    op.drop_column("category", "color")
    _create_optional_readonly_view(include_active=True)


def _drop_optional_readonly_view() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'finance_ai_view') THEN
                DROP VIEW IF EXISTS finance_ai_view.category;
            END IF;
        END $$
        """
    )


def _create_optional_readonly_view(*, include_active: bool) -> None:
    columns = (
        "id, user_id, name, is_active, created_at, updated_at"
        if include_active
        else "id, user_id, name, color, icon, sort_order, created_at, updated_at"
    )
    op.execute(
        f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'finance_ai_view') THEN
                EXECUTE 'CREATE VIEW finance_ai_view.category AS '
                    || 'SELECT {columns} FROM public.category';
                GRANT SELECT ON finance_ai_view.category TO finance_ai_viewer;
            END IF;
        END $$
        """
    )

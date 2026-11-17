"""Store support attachments alongside their tickets and replies."""

import sqlalchemy as sa
from alembic import op

revision = "0024_support_attachments"
down_revision = "0023_support_ticket_routines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_attachment",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.BigInteger(),
            sa.ForeignKey("support_ticket.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("support_ticket_message.id", ondelete="CASCADE"),
            unique=True,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
    )
    op.create_index("ix_support_attachment_ticket_id", "support_attachment", ["ticket_id"])


def downgrade() -> None:
    op.drop_table("support_attachment")

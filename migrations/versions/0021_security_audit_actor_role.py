"""Record the actor's role at event time on security audit events.

The role is derived inside PostgreSQL from the actor's account when the event is inserted, so
application code cannot supply or spoof it and later role changes never rewrite history. Events
recorded before this revision keep a null role ("not recorded"); the append-only table is never
backfilled.

Revision ID: 0021_audit_actor_role
Revises: 0020_admin_account_management
Create Date: 2026-11-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_audit_actor_role"
down_revision: str | Sequence[str] | None = "0020_admin_account_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DERIVE_FUNCTION = """
CREATE FUNCTION security_audit_event_derive_actor_role() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Never trust a supplied role: it always comes from the actor's account at insert time.
    NEW.actor_role := NULL;
    IF NEW.actor_id ~ '^[1-9][0-9]{0,18}$' THEN
        SELECT role.name INTO NEW.actor_role
        FROM app_user JOIN role ON role.id = app_user.role_id
        WHERE app_user.id = NEW.actor_id::bigint;
    END IF;
    RETURN NEW;
END;
$$
"""

_DERIVE_TRIGGER = """
CREATE TRIGGER security_audit_event_actor_role
BEFORE INSERT ON security_audit_event
FOR EACH ROW EXECUTE FUNCTION security_audit_event_derive_actor_role()
"""


def upgrade() -> None:
    op.add_column("security_audit_event", sa.Column("actor_role", sa.String(length=32)))
    op.create_check_constraint(
        op.f("ck_security_audit_event_actor_role_known"),
        "security_audit_event",
        "actor_role IS NULL OR actor_role IN "
        "('user', 'support_agent', 'admin', 'security_auditor')",
    )
    op.create_index(
        "ix_security_audit_event_actor_role_chronology",
        "security_audit_event",
        ["actor_role", sa.text("occurred_at DESC"), sa.text("event_id DESC")],
    )
    op.execute(_DERIVE_FUNCTION)
    op.execute(_DERIVE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS security_audit_event_actor_role ON security_audit_event")
    op.execute("DROP FUNCTION IF EXISTS security_audit_event_derive_actor_role()")
    op.drop_index(
        "ix_security_audit_event_actor_role_chronology", table_name="security_audit_event"
    )
    op.drop_constraint(
        op.f("ck_security_audit_event_actor_role_known"), "security_audit_event", type_="check"
    )
    op.drop_column("security_audit_event", "actor_role")

"""Install the support agent's ticket triggers and procedures.

The triggers keep the queue's activity timestamp current and enforce a nonempty
resolution summary even for SQL writes outside the API. The procedures accept
an agent ID, a ticket ID and a reason or summary. CALL returns an `applied` flag.
The caller owns the transaction, so the API can commit the security audit event
together with the ticket update and its activity log.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_support_ticket_routines"
down_revision: str | Sequence[str] | None = "0022_audit_routines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # T1: a new message moves the ticket up the queue. Imports of older messages
    # must never move its activity timestamp backwards.
    op.execute("""
        CREATE FUNCTION support_ticket_touch_on_message() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE support_ticket
            SET updated_at = NEW.created_at
            WHERE id = NEW.ticket_id AND updated_at < NEW.created_at;
            RETURN NULL;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER support_ticket_touch_on_message
        AFTER INSERT ON support_ticket_message
        FOR EACH ROW EXECUTE FUNCTION support_ticket_touch_on_message()
    """)

    # T2: validate new resolutions and edits to their summaries. Updating other
    # columns on historical tickets does not retroactively validate old data.
    op.execute("""
        CREATE FUNCTION support_ticket_require_resolution() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF coalesce(NEW.resolution_summary, '') !~ '[^[:space:]]' THEN
                RAISE EXCEPTION 'a resolved ticket requires a resolution summary'
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'support_ticket_resolution_required';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER support_ticket_require_resolution
        BEFORE INSERT OR UPDATE OF status, resolution_summary ON support_ticket
        FOR EACH ROW WHEN (NEW.status = 'resolved')
        EXECUTE FUNCTION support_ticket_require_resolution()
    """)

    # P1: move a ticket from in_progress to escalated and keep the reason in
    # staff-only ticket activity. Record the original agent for admin handoff.
    op.execute("""
        CREATE PROCEDURE sp_support_escalate_ticket(
            IN p_actor_id bigint,
            IN p_ticket_id bigint,
            IN p_reason text,
            OUT applied boolean
        )
        LANGUAGE plpgsql AS $$
        BEGIN
            applied := FALSE;
            IF coalesce(p_reason, '') !~ '[^[:space:]]' OR char_length(p_reason) > 4000 THEN
                RETURN;
            END IF;
            UPDATE support_ticket
            SET status = 'escalated',
                originating_support_agent_id = p_actor_id,
                updated_at = greatest(updated_at, clock_timestamp())
            WHERE id = p_ticket_id
              AND status = 'in_progress'
              AND assigned_staff_user_id = p_actor_id
              AND EXISTS (
                  SELECT 1 FROM app_user u JOIN role r ON r.id = u.role_id
                  WHERE u.id = p_actor_id AND u.is_active AND r.name = 'support_agent'
              );
            IF NOT FOUND THEN
                RETURN;
            END IF;

            INSERT INTO support_ticket_activity (
                ticket_id, actor_user_id, event_type, previous_status, new_status,
                internal_reason, created_at
            ) VALUES (
                p_ticket_id, p_actor_id, 'ticket_escalated', 'in_progress', 'escalated',
                p_reason, clock_timestamp()
            );
            applied := TRUE;
        END;
        $$
    """)

    # P2: move a ticket from in_progress to resolved. The customer can see the
    # summary on the ticket. The activity log only stores the state change.
    op.execute("""
        CREATE PROCEDURE sp_support_resolve_ticket(
            IN p_actor_id bigint,
            IN p_ticket_id bigint,
            IN p_summary text,
            OUT applied boolean
        )
        LANGUAGE plpgsql AS $$
        BEGIN
            applied := FALSE;
            IF coalesce(p_summary, '') !~ '[^[:space:]]' OR char_length(p_summary) > 4000 THEN
                RETURN;
            END IF;
            UPDATE support_ticket
            SET status = 'resolved',
                resolution_summary = p_summary,
                updated_at = greatest(updated_at, clock_timestamp())
            WHERE id = p_ticket_id
              AND status = 'in_progress'
              AND assigned_staff_user_id = p_actor_id
              AND EXISTS (
                  SELECT 1 FROM app_user u JOIN role r ON r.id = u.role_id
                  WHERE u.id = p_actor_id AND u.is_active AND r.name = 'support_agent'
              );
            IF NOT FOUND THEN
                RETURN;
            END IF;

            INSERT INTO support_ticket_activity (
                ticket_id, actor_user_id, event_type, previous_status, new_status, created_at
            ) VALUES (
                p_ticket_id, p_actor_id, 'ticket_resolved', 'in_progress', 'resolved',
                clock_timestamp()
            );
            applied := TRUE;
        END;
        $$
    """)


def downgrade() -> None:
    op.execute("DROP PROCEDURE sp_support_resolve_ticket(bigint, bigint, text)")
    op.execute("DROP PROCEDURE sp_support_escalate_ticket(bigint, bigint, text)")
    op.execute("DROP TRIGGER support_ticket_require_resolution ON support_ticket")
    op.execute("DROP FUNCTION support_ticket_require_resolution()")
    op.execute("DROP TRIGGER support_ticket_touch_on_message ON support_ticket_message")
    op.execute("DROP FUNCTION support_ticket_touch_on_message()")

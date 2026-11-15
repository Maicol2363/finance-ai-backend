"""Administrator role: MFA recovery, session role binding and account management.

Revision ID: 0020_admin_account_management
Revises: 0019_staff_mfa_challenge
Create Date: 2026-11-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_admin_account_management"
down_revision: str | Sequence[str] | None = "0019_staff_mfa_challenge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Database routines (documented in docs/DB-ROUTINES-ADMIN.md) -----------------------------

# T1: no change may leave the system without an active administrator (ADM-02 #8-10). The
# advisory lock serializes competing changes; the count then runs on a fresh READ COMMITTED
# snapshot, so two concurrent removals cannot both pass a stale count. Any other isolation
# level fails closed.
LAST_ADMIN_GUARD_FUNCTION = """
CREATE FUNCTION app_user_last_admin_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_admin_role_id bigint;
    v_remaining integer;
BEGIN
    SELECT id INTO v_admin_role_id FROM role WHERE name = 'admin';
    IF v_admin_role_id IS NULL OR NOT (OLD.role_id = v_admin_role_id AND OLD.is_active) THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.role_id = v_admin_role_id AND NEW.is_active THEN
        RETURN NEW;
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'administrator changes require READ COMMITTED isolation'
            USING ERRCODE = 'feature_not_supported';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('finance_ai.active_admin_invariant', 0));
    SELECT count(*) INTO v_remaining
    FROM app_user
    WHERE role_id = v_admin_role_id AND is_active AND id <> OLD.id;
    IF v_remaining = 0 THEN
        RAISE EXCEPTION 'the last active administrator cannot be removed'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'last_active_admin';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$
"""

LAST_ADMIN_GUARD_TRIGGER = """
CREATE TRIGGER app_user_last_admin_guard
BEFORE UPDATE OF is_active, role_id OR DELETE ON app_user
FOR EACH ROW EXECUTE FUNCTION app_user_last_admin_guard()
"""

# T2: suspending an account or changing its role immediately revokes every live session
# of that account, whichever code path made the change (ADM-02 #4, ADM-04 #8). Reactivation
# never restores a session (ADM-02 #6).
REVOKE_SESSIONS_FUNCTION = """
CREATE FUNCTION app_user_revoke_sessions_on_authority_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_reason text;
BEGIN
    IF OLD.is_active AND NOT NEW.is_active THEN
        v_reason := 'suspended';
    ELSIF OLD.role_id IS DISTINCT FROM NEW.role_id THEN
        v_reason := 'role_changed';
    ELSE
        RETURN NULL;
    END IF;
    UPDATE user_session
    SET revoked_at = CURRENT_TIMESTAMP, revocation_reason = v_reason
    WHERE user_id = NEW.id AND revoked_at IS NULL;
    RETURN NULL;
END;
$$
"""

REVOKE_SESSIONS_TRIGGER = """
CREATE TRIGGER app_user_revoke_sessions_on_authority_change
AFTER UPDATE OF is_active, role_id ON app_user
FOR EACH ROW
WHEN (OLD.is_active IS DISTINCT FROM NEW.is_active OR OLD.role_id IS DISTINCT FROM NEW.role_id)
EXECUTE FUNCTION app_user_revoke_sessions_on_authority_change()
"""

# P1: atomic suspend / reactivate. Result codes: ok, actor_not_authorized, self_target,
# not_found, invalid_reason, already_in_state, last_admin.
SET_ACCOUNT_SUSPENSION_PROCEDURE = """
CREATE PROCEDURE sp_admin_set_account_suspension(
    IN p_actor_id bigint,
    IN p_target_id bigint,
    IN p_suspend boolean,
    IN p_reason text,
    INOUT p_result text DEFAULT NULL
)
LANGUAGE plpgsql AS $$
DECLARE
    v_target app_user%ROWTYPE;
    v_reason text;
    v_constraint text;
BEGIN
    -- Lock the actor and the target together, in ascending id order, whichever role each plays
    -- in this call. Two administrators acting on each other (or on a shared target) therefore
    -- queue behind one another instead of each holding the row the other needs. NO KEY UPDATE
    -- does not conflict with the foreign-key checks (FOR KEY SHARE) that the inserts below make
    -- on these same rows; FOR UPDATE did, and that closed a deadlock cycle. The actor is judged
    -- only after the locks, on the latest committed state: an administrator suspended a moment
    -- earlier by a concurrent call is no longer authorized.
    PERFORM 1 FROM app_user WHERE id IN (p_actor_id, p_target_id) ORDER BY id FOR NO KEY UPDATE;
    IF NOT EXISTS (
        SELECT 1 FROM app_user u JOIN role r ON r.id = u.role_id
        WHERE u.id = p_actor_id AND u.is_active AND r.name = 'admin'
    ) THEN
        p_result := 'actor_not_authorized';
        RETURN;
    END IF;
    IF p_suspend AND p_actor_id = p_target_id THEN
        p_result := 'self_target';
        RETURN;
    END IF;
    SELECT * INTO v_target FROM app_user WHERE id = p_target_id;
    IF NOT FOUND THEN
        p_result := 'not_found';
        RETURN;
    END IF;
    IF p_suspend THEN
        v_reason := btrim(coalesce(p_reason, ''));
        IF v_reason = '' OR char_length(v_reason) > 500 THEN
            p_result := 'invalid_reason';
            RETURN;
        END IF;
        IF NOT v_target.is_active THEN
            p_result := 'already_in_state';
            RETURN;
        END IF;
        BEGIN
            UPDATE app_user SET is_active = false WHERE id = p_target_id;
        EXCEPTION WHEN check_violation THEN
            GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME;
            IF v_constraint = 'last_active_admin' THEN
                p_result := 'last_admin';
                RETURN;
            END IF;
            RAISE;
        END;
        INSERT INTO account_suspension (user_id, suspended_by_user_id, reason)
        VALUES (p_target_id, p_actor_id, v_reason);
    ELSE
        IF v_target.is_active THEN
            p_result := 'already_in_state';
            RETURN;
        END IF;
        UPDATE app_user SET is_active = true WHERE id = p_target_id;
        UPDATE account_suspension
        SET reactivated_by_user_id = p_actor_id, reactivated_at = CURRENT_TIMESTAMP
        WHERE user_id = p_target_id AND reactivated_at IS NULL;
    END IF;
    p_result := 'ok';
END;
$$
"""


# P2: atomic return of an escalated ticket to Support (ADM-05 #9-10). The originating agent is
# re-evaluated inside the transaction: still an active support_agent -> that agent and
# in_progress; otherwise unassigned and open. Status, assignment, ownership and the activity
# row change together or not at all. Result codes: ok, actor_not_authorized, invalid_reason,
# not_found, not_escalated, not_owner.
RETURN_ESCALATED_TICKET_PROCEDURE = """
CREATE PROCEDURE sp_admin_return_escalated_ticket(
    IN p_admin_id bigint,
    IN p_ticket_id bigint,
    IN p_reason text,
    INOUT p_result text DEFAULT NULL,
    INOUT p_new_status text DEFAULT NULL,
    INOUT p_new_assignee bigint DEFAULT NULL
)
LANGUAGE plpgsql AS $$
DECLARE
    v_ticket support_ticket%ROWTYPE;
    v_reason text;
    v_origin_eligible boolean;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM app_user u JOIN role r ON r.id = u.role_id
        WHERE u.id = p_admin_id AND u.is_active AND r.name = 'admin'
    ) THEN
        p_result := 'actor_not_authorized';
        RETURN;
    END IF;
    v_reason := btrim(coalesce(p_reason, ''));
    IF v_reason = '' OR char_length(v_reason) > 4000 THEN
        p_result := 'invalid_reason';
        RETURN;
    END IF;
    SELECT * INTO v_ticket FROM support_ticket WHERE id = p_ticket_id FOR UPDATE;
    IF NOT FOUND THEN
        p_result := 'not_found';
        RETURN;
    END IF;
    IF v_ticket.status <> 'escalated' THEN
        p_result := 'not_escalated';
        RETURN;
    END IF;
    IF v_ticket.admin_owner_user_id IS DISTINCT FROM p_admin_id THEN
        p_result := 'not_owner';
        RETURN;
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM app_user u JOIN role r ON r.id = u.role_id
        WHERE u.id = v_ticket.originating_support_agent_id
          AND u.is_active AND r.name = 'support_agent'
    ) INTO v_origin_eligible;
    IF v_origin_eligible THEN
        p_new_status := 'in_progress';
        p_new_assignee := v_ticket.originating_support_agent_id;
    ELSE
        p_new_status := 'open';
        p_new_assignee := NULL;
    END IF;
    UPDATE support_ticket
    SET status = p_new_status,
        assigned_staff_user_id = p_new_assignee,
        admin_owner_user_id = NULL,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_ticket_id;
    INSERT INTO support_ticket_activity (
        ticket_id, actor_user_id, event_type, previous_status, new_status,
        previous_assignee_user_id, new_assignee_user_id, internal_reason
    ) VALUES (
        p_ticket_id, p_admin_id, 'ticket_returned', 'escalated', p_new_status,
        v_ticket.assigned_staff_user_id, p_new_assignee, v_reason
    );
    p_result := 'ok';
END;
$$
"""


def upgrade() -> None:
    # A staff session remembers the role it was issued for, so a role change can never carry
    # an old (possibly MFA-verified) session over to different authority (SEC-01, ADM-04).
    op.add_column("user_session", sa.Column("issued_role_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_user_session_issued_role_id_role",
        "user_session",
        "role",
        ["issued_role_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "staff_mfa_recovery_code",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("char_length(code_hash) = 64", name="code_hash_length"),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumption_after_creation"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_staff_mfa_recovery_code_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staff_mfa_recovery_code"),
        sa.UniqueConstraint("code_hash", name="uq_staff_mfa_recovery_code_code_hash"),
    )
    op.create_index(
        "ix_staff_mfa_recovery_code_user_open",
        "staff_mfa_recovery_code",
        ["user_id", "consumed_at"],
    )

    op.create_table(
        "account_suspension",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("suspended_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "suspended_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("reactivated_by_user_id", sa.BigInteger()),
        sa.Column("reactivated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0 AND char_length(reason) <= 500", name="reason_bounded"
        ),
        sa.CheckConstraint(
            "(reactivated_at IS NULL AND reactivated_by_user_id IS NULL) OR "
            "(reactivated_at IS NOT NULL AND reactivated_by_user_id IS NOT NULL)",
            name="reactivation_pair",
        ),
        sa.CheckConstraint(
            "reactivated_at IS NULL OR reactivated_at >= suspended_at",
            name="reactivation_after_suspension",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_account_suspension_user_id_app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["suspended_by_user_id"],
            ["app_user.id"],
            name="fk_account_suspension_suspended_by_user_id_app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reactivated_by_user_id"],
            ["app_user.id"],
            name="fk_account_suspension_reactivated_by_user_id_app_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account_suspension"),
    )
    op.create_index(
        "uq_account_suspension_open",
        "account_suspension",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("reactivated_at IS NULL"),
    )
    op.create_index(
        "ix_account_suspension_user_history", "account_suspension", ["user_id", "suspended_at"]
    )

    op.execute(LAST_ADMIN_GUARD_FUNCTION)
    op.execute(LAST_ADMIN_GUARD_TRIGGER)
    op.execute(REVOKE_SESSIONS_FUNCTION)
    op.execute(REVOKE_SESSIONS_TRIGGER)
    op.execute(SET_ACCOUNT_SUSPENSION_PROCEDURE)

    # ADM-05: an administrator claims an escalated case without displacing the originating
    # agent (kept in assigned_staff_user_id / originating_support_agent_id for the return).
    op.add_column(
        "support_ticket", sa.Column("admin_owner_user_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_support_ticket_admin_owner_user_id_app_user",
        "support_ticket",
        "app_user",
        ["admin_owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "admin_owner_only_when_escalated",
        "support_ticket",
        "admin_owner_user_id IS NULL OR status = 'escalated'",
    )
    op.create_index(
        "ix_support_ticket_escalated_queue",
        "support_ticket",
        ["created_at", "id"],
        postgresql_where=sa.text("status = 'escalated'"),
    )
    op.execute(RETURN_ESCALATED_TICKET_PROCEDURE)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        history = op.get_bind().scalar(sa.text("SELECT count(*) FROM account_suspension"))
        if history:
            raise RuntimeError(
                "Refusing to drop account_suspension while it contains suspension history"
            )
    op.execute(
        "DROP PROCEDURE IF EXISTS sp_admin_return_escalated_ticket"
        "(bigint, bigint, text, text, text, bigint)"
    )
    op.drop_index("ix_support_ticket_escalated_queue", table_name="support_ticket")
    op.drop_constraint("admin_owner_only_when_escalated", "support_ticket", type_="check")
    op.drop_constraint(
        "fk_support_ticket_admin_owner_user_id_app_user", "support_ticket", type_="foreignkey"
    )
    op.drop_column("support_ticket", "admin_owner_user_id")
    op.execute(
        "DROP PROCEDURE IF EXISTS sp_admin_set_account_suspension"
        "(bigint, bigint, boolean, text, text)"
    )
    op.execute("DROP TRIGGER IF EXISTS app_user_revoke_sessions_on_authority_change ON app_user")
    op.execute("DROP FUNCTION IF EXISTS app_user_revoke_sessions_on_authority_change()")
    op.execute("DROP TRIGGER IF EXISTS app_user_last_admin_guard ON app_user")
    op.execute("DROP FUNCTION IF EXISTS app_user_last_admin_guard()")
    op.drop_index("ix_account_suspension_user_history", table_name="account_suspension")
    op.drop_index("uq_account_suspension_open", table_name="account_suspension")
    op.drop_table("account_suspension")
    op.drop_index("ix_staff_mfa_recovery_code_user_open", table_name="staff_mfa_recovery_code")
    op.drop_table("staff_mfa_recovery_code")
    op.drop_constraint("fk_user_session_issued_role_id_role", "user_session", type_="foreignkey")
    op.drop_column("user_session", "issued_role_id")

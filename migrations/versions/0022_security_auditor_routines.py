"""Security Auditor database routines: two SEC-01 guard triggers and two SEC-02 procedures.

No new behavior: each routine enforces, inside PostgreSQL, a rule the application already
follows for the Security Auditor (AUD-04 delivery, role control, one-time MFA challenges and
TOTP replay protection), so a defect or a manual change cannot bypass it.

Triggers
- ``staff_mfa_challenge_one_time_guard``: a consumed sign-in challenge is never reopened; its
  owner, token and lifetime never change; failed attempts never decrease.
- ``admin_totp_factor_replay_guard``: the last accepted TOTP time step only moves forward, the
  owner and secret never change, and a disabled authenticator is never reactivated.

Procedures
- ``sp_audit_record_report_export``: re-checks, under a share lock on the auditor's account,
  that it is an active, authoritatively activated ``security_auditor`` and records
  ``audit.report_exported`` in the same transaction (AUD-04: authority at delivery plus durable
  evidence before the PDF is returned).
- ``sp_audit_record_access_denied``: records ``access.denied`` with a fixed shape from an
  enumerated resource and reason, so no request content can reach the evidence.

Revision ID: 0022_audit_routines
Revises: 0021_audit_actor_role
Create Date: 2026-11-16 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022_audit_routines"
down_revision: str | Sequence[str] | None = "0021_audit_actor_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHALLENGE_GUARD = """
CREATE FUNCTION staff_mfa_challenge_one_time_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.user_id IS DISTINCT FROM OLD.user_id
        OR NEW.token_hash IS DISTINCT FROM OLD.token_hash
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'an MFA challenge keeps its account, token and lifetime'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.consumed_at IS NOT NULL AND (
        NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
        OR NEW.failed_attempts IS DISTINCT FROM OLD.failed_attempts
    ) THEN
        RAISE EXCEPTION 'a consumed MFA challenge cannot be reopened or reused'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.failed_attempts < OLD.failed_attempts THEN
        RAISE EXCEPTION 'MFA challenge failed attempts never decrease'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

_FACTOR_GUARD = """
CREATE FUNCTION admin_totp_factor_replay_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.admin_user_id IS DISTINCT FROM OLD.admin_user_id
        OR NEW.secret_ciphertext IS DISTINCT FROM OLD.secret_ciphertext
        OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'an authenticator keeps its account and secret'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.last_accepted_time_step IS NOT NULL AND (
        NEW.last_accepted_time_step IS NULL
        OR NEW.last_accepted_time_step < OLD.last_accepted_time_step
    ) THEN
        RAISE EXCEPTION 'an accepted TOTP time step can never be accepted again'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
        (OLD.state = 'pending' AND NEW.state IN ('active', 'disabled'))
        OR (OLD.state = 'active' AND NEW.state = 'disabled')
    ) THEN
        RAISE EXCEPTION 'authenticator state cannot move from % to %', OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

_REPORT_EXPORT = """
CREATE PROCEDURE sp_audit_record_report_export(
    p_auditor_id bigint,
    p_report_id uuid,
    p_correlation_id text,
    p_occurred_at timestamptz,
    p_context jsonb
)
LANGUAGE plpgsql AS $$
DECLARE
    v_active boolean;
    v_role text;
    v_email text;
BEGIN
    -- A share lock makes a concurrent suspension or role change (an UPDATE of this row) wait
    -- for this transaction, so the authority checked here is the one the evidence records.
    SELECT app_user.is_active, role.name, app_user.email
      INTO v_active, v_role, v_email
      FROM app_user JOIN role ON role.id = app_user.role_id
     WHERE app_user.id = p_auditor_id
       FOR SHARE OF app_user;
    IF NOT FOUND
        OR NOT v_active
        OR v_role <> 'security_auditor'
        OR NOT EXISTS (
            SELECT 1
              FROM staff_invitation JOIN role ON role.id = staff_invitation.role_id
             WHERE staff_invitation.activated_user_id = p_auditor_id
               AND staff_invitation.consumed_at IS NOT NULL
               AND staff_invitation.email = v_email
               AND role.name IN ('support_agent', 'security_auditor')
        ) THEN
        RAISE EXCEPTION 'audit report delivery is not authorized'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF jsonb_typeof(p_context) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'export context must be a JSON object' USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO security_audit_event (
        event_id, event_type, actor_id, target_resource_type, target_resource_id,
        occurred_at, result, correlation_id, context
    ) VALUES (
        gen_random_uuid(), 'audit.report_exported', p_auditor_id::text, 'audit_report',
        p_report_id::text, p_occurred_at, 'success', p_correlation_id, p_context
    );
END;
$$
"""

_ACCESS_DENIED = """
CREATE PROCEDURE sp_audit_record_access_denied(
    p_actor_id bigint,
    p_resource text,
    p_reason text,
    p_correlation_id text,
    p_occurred_at timestamptz
)
LANGUAGE plpgsql AS $$
BEGIN
    IF p_resource NOT IN ('support', 'security_audit', 'administration')
        OR p_reason NOT IN ('role_not_permitted') THEN
        RAISE EXCEPTION 'unapproved access denial resource or reason'
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO security_audit_event (
        event_id, event_type, actor_id, target_resource_type, target_resource_id,
        occurred_at, result, correlation_id, context
    ) VALUES (
        gen_random_uuid(), 'access.denied', p_actor_id::text, 'protected_resource', p_resource,
        p_occurred_at, 'denied', p_correlation_id,
        jsonb_build_object('reason', p_reason, 'resource', p_resource)
    );
END;
$$
"""


def upgrade() -> None:
    op.execute(_CHALLENGE_GUARD)
    op.execute(
        "CREATE TRIGGER staff_mfa_challenge_one_time_guard BEFORE UPDATE ON staff_mfa_challenge "
        "FOR EACH ROW EXECUTE FUNCTION staff_mfa_challenge_one_time_guard()"
    )
    op.execute(_FACTOR_GUARD)
    op.execute(
        "CREATE TRIGGER admin_totp_factor_replay_guard BEFORE UPDATE ON admin_totp_factor "
        "FOR EACH ROW EXECUTE FUNCTION admin_totp_factor_replay_guard()"
    )
    op.execute(_REPORT_EXPORT)
    op.execute(_ACCESS_DENIED)


def downgrade() -> None:
    op.execute(
        "DROP PROCEDURE IF EXISTS sp_audit_record_access_denied("
        "bigint, text, text, text, timestamptz)"
    )
    op.execute(
        "DROP PROCEDURE IF EXISTS sp_audit_record_report_export("
        "bigint, uuid, text, timestamptz, jsonb)"
    )
    op.execute("DROP TRIGGER IF EXISTS admin_totp_factor_replay_guard ON admin_totp_factor")
    op.execute("DROP FUNCTION IF EXISTS admin_totp_factor_replay_guard()")
    op.execute("DROP TRIGGER IF EXISTS staff_mfa_challenge_one_time_guard ON staff_mfa_challenge")
    op.execute("DROP FUNCTION IF EXISTS staff_mfa_challenge_one_time_guard()")

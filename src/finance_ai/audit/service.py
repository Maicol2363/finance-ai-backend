"""Shared security audit services."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.core.logging import get_logger
from finance_ai.db.models.audit import SecurityAuditEvent

logger = get_logger(__name__)

# Report export evidence: acting Auditor, outcome, report ID, period, count and the
# sanitized filter allowlist. Never PDF contents, credentials or download URLs.
REPORT_EXPORT_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "reason",
        "report_id",
        "period_start",
        "period_end",
        "time_zone",
        "event_count",
        "filter_event_type",
        "filter_outcome",
        "filter_actor_id",
        "filter_target_type",
        "filter_target_id",
        "filter_correlation_id",
        "filter_actor_role",
    }
)
# Customer-app sign-in evidence: where it came from, never the email address or credentials.
CUSTOMER_LOGIN_CONTEXT_KEYS: Final[frozenset[str]] = frozenset({"reason", "channel", "platform"})
_DEFAULT_CONTEXT_KEYS: Final[frozenset[str]] = frozenset({"reason"})
_CONTEXT_KEYS_BY_EVENT: Final[dict[str, frozenset[str]]] = {
    "access.denied": frozenset({"reason", "resource"}),
    "auth.login_success": CUSTOMER_LOGIN_CONTEXT_KEYS,
    "auth.login_failed": CUSTOMER_LOGIN_CONTEXT_KEYS,
    "auth.logout": CUSTOMER_LOGIN_CONTEXT_KEYS,
    # ADM-04: the role.changed event carries previous and new role only.
    "role.changed": frozenset({"previous_role", "new_role"}),
    # ADM-03: the staff role being granted; never the invitation token or the email address.
    "staff.created": frozenset({"new_role"}),
    "staff.activated": frozenset({"new_role"}),
    "audit.report_exported": REPORT_EXPORT_CONTEXT_KEYS,
}
_ALLOWED_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "staff.login",
        "staff.login_denied",
        "staff.refresh",
        "staff.session_revoked",
        "staff.logout",
        "support.ticket_claimed",
        "support.ticket_created",
        "support.message_sent",
        "ticket.escalated",
        "support.ticket_resolved",
        "support.ticket_returned",
        "support.customer_lookup",
        "auth.login_success",
        "auth.login_failed",
        "auth.logout",
        "auth.mfa_success",
        "auth.mfa_failed",
        "auth.mfa_enrolled",
        "auth.mfa_recovered",
        "auth.mfa_replaced",
        "access.denied",
        "audit.report_exported",
        "account.suspended",
        "account.reactivated",
        "staff.created",
        "staff.activated",
        "role.changed",
    }
)
_ALLOWED_RESULTS: Final[frozenset[str]] = frozenset({"success", "denied", "failure"})
_ALLOWED_TARGET_TYPES: Final[frozenset[str]] = frozenset(
    {
        "staff_session",
        "support_ticket",
        "account",
        "audit_report",
        "protected_resource",
        "staff_invitation",
    }
)
_MAX_CONTEXT_VALUE_LENGTH: Final = 128


class SecurityAuditWriter:
    """Append sanitized security evidence in the caller's transaction."""

    @staticmethod
    def validate(
        *, event_type: str, result: str, context: dict[str, str], target_type: str | None
    ) -> None:
        """Reject anything outside the SEC-02 allowlist before it can reach the evidence."""
        allowed_keys = _CONTEXT_KEYS_BY_EVENT.get(event_type, _DEFAULT_CONTEXT_KEYS)
        if (
            event_type not in _ALLOWED_EVENT_TYPES
            or set(context) - allowed_keys
            or result not in _ALLOWED_RESULTS
            or (target_type is not None and target_type not in _ALLOWED_TARGET_TYPES)
            or any(
                not isinstance(value, str) or len(value) > _MAX_CONTEXT_VALUE_LENGTH
                for value in context.values()
            )
        ):
            raise ValueError("Audit event contains an unapproved field")

    def append(
        self,
        session: Session,
        *,
        event_type: str,
        actor_id: str | None,
        result: str,
        correlation_id: str,
        context: dict[str, str],
        target_id: str | None = None,
        target_type: str | None = None,
    ) -> None:
        self.validate(
            event_type=event_type, result=result, context=context, target_type=target_type
        )
        resolved_target_type = (
            None
            if target_id is None
            else target_type
            if target_type is not None
            else "staff_session"
            if event_type.startswith("staff.")
            else "support_ticket"
        )
        session.add(
            SecurityAuditEvent(
                event_type=event_type,
                actor_id=actor_id,
                target_resource_type=resolved_target_type,
                target_resource_id=target_id,
                occurred_at=datetime.now(UTC),
                result=result,
                correlation_id=correlation_id,
                context=context,
            )
        )


# Database-defined denial evidence (migration 0022): only these enumerated values are accepted.
_RECORD_ACCESS_DENIED = text(
    "CALL sp_audit_record_access_denied("
    ":actor_id, :resource, :reason, :correlation_id, :occurred_at)"
)


class AccessDenialRecorder:
    """Record ``access.denied`` for an authenticated principal without request contents.

    The stored procedure ``sp_audit_record_access_denied`` builds the event from an enumerated
    resource and reason only, so no request content can reach the evidence.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record(self, *, actor_id: int, resource: str, reason: str, correlation_id: str) -> None:
        try:
            with self._session_factory() as session, session.begin():
                session.execute(
                    _RECORD_ACCESS_DENIED,
                    {
                        "actor_id": actor_id,
                        "resource": resource,
                        "reason": reason,
                        "correlation_id": correlation_id,
                        "occurred_at": datetime.now(UTC),
                    },
                )
        except Exception as exc:
            # The request is still denied: a logging failure never turns into access.
            logger.warning(
                "Access denial could not be recorded",
                extra={
                    "correlation_id": correlation_id,
                    "event": "audit.access_denied_unrecorded",
                    "error_type": type(exc).__name__,
                    "outcome": "denied",
                },
            )

"""Shared security audit services."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

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
    }
)
_DEFAULT_CONTEXT_KEYS: Final[frozenset[str]] = frozenset({"reason"})
_CONTEXT_KEYS_BY_EVENT: Final[dict[str, frozenset[str]]] = {
    "access.denied": frozenset({"reason", "resource"}),
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
        "support.customer_lookup",
        "auth.login_success",
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
        "session.revoked",
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


class AccessDenialRecorder:
    """Record ``access.denied`` for an authenticated principal without request contents."""

    def __init__(self, session_factory: sessionmaker[Session], writer: SecurityAuditWriter) -> None:
        self._session_factory = session_factory
        self._writer = writer

    def record(self, *, actor_id: int, resource: str, reason: str, correlation_id: str) -> None:
        try:
            with self._session_factory() as session, session.begin():
                self._writer.append(
                    session,
                    event_type="access.denied",
                    actor_id=str(actor_id),
                    target_id=resource,
                    target_type="protected_resource",
                    result="denied",
                    correlation_id=correlation_id,
                    context={"reason": reason, "resource": resource},
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

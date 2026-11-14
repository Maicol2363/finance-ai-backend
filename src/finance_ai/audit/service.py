from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy.orm import Session

from finance_ai.db.models.audit import SecurityAuditEvent

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
    }
)
_ALLOWED_CONTEXT_KEYS: Final[frozenset[str]] = frozenset({"reason"})


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
    ) -> None:
        if event_type not in _ALLOWED_EVENT_TYPES or set(context) - _ALLOWED_CONTEXT_KEYS:
            raise ValueError("Audit event contains an unapproved field")
        session.add(
            SecurityAuditEvent(
                event_type=event_type,
                actor_id=actor_id,
                target_resource_type=(
                    "staff_session" if event_type.startswith("staff.") else "support_ticket"
                )
                if target_id is not None
                else None,
                target_resource_id=target_id,
                occurred_at=datetime.now(UTC),
                result=result,
                correlation_id=correlation_id,
                context=context,
            )
        )

"""Shared helpers for Security Auditor read/report tests (fixtures live in conftest.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from finance_ai.db.models.audit import SecurityAuditEvent


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def add_event(
    factory: sessionmaker[Session],
    *,
    occurred_at: datetime,
    event_type: str = "staff.login",
    result: str = "success",
    actor_id: str | None = "7",
    target_type: str | None = None,
    target_id: str | None = None,
    correlation_id: str | None = "corr-1",
    context: dict[str, Any] | None = None,
    event_id: UUID | None = None,
) -> UUID:
    identifier = event_id or uuid4()
    with factory() as session, session.begin():
        session.add(
            SecurityAuditEvent(
                event_id=identifier,
                event_type=event_type,
                actor_id=actor_id,
                target_resource_type=target_type,
                target_resource_id=target_id,
                occurred_at=occurred_at.astimezone(UTC),
                result=result,
                correlation_id=correlation_id,
                context=context or {},
            )
        )
    return identifier

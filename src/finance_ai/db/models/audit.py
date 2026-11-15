from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from finance_ai.db.base import Base


class SecurityAuditEvent(Base):
    __tablename__ = "security_audit_event"

    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    target_resource_type: Mapped[str | None] = mapped_column(String(100))
    target_resource_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    context: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )


Index(
    "ix_security_audit_event_chronology",
    SecurityAuditEvent.occurred_at.desc(),
    SecurityAuditEvent.event_id.desc(),
)
Index(
    "ix_security_audit_event_actor_chronology",
    SecurityAuditEvent.actor_id,
    SecurityAuditEvent.occurred_at.desc(),
    SecurityAuditEvent.event_id.desc(),
)
Index(
    "ix_security_audit_event_target_chronology",
    SecurityAuditEvent.target_resource_type,
    SecurityAuditEvent.target_resource_id,
    SecurityAuditEvent.occurred_at.desc(),
    SecurityAuditEvent.event_id.desc(),
)
Index(
    "ix_security_audit_event_type_chronology",
    SecurityAuditEvent.event_type,
    SecurityAuditEvent.occurred_at.desc(),
    SecurityAuditEvent.event_id.desc(),
)
Index(
    "ix_security_audit_event_result_chronology",
    SecurityAuditEvent.result,
    SecurityAuditEvent.occurred_at.desc(),
    SecurityAuditEvent.event_id.desc(),
)

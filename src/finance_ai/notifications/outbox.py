"""Writing to the notification outbox.

``enqueue`` is meant to be called inside the transaction of the event that caused the
notification, so the decision commits or rolls back together with it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from finance_ai.db.models.notifications import NOTIFICATION_KINDS, NotificationOutbox


def enqueue(
    session: Session,
    *,
    user_id: int,
    kind: str,
    dedupe_key: str,
    payload: dict[str, Any],
    status: str = "pending",
    reason: str | None = None,
    not_before: datetime | None = None,
) -> bool:
    """Record a notification unless one with the same ``dedupe_key`` already exists.

    Returns ``True`` when a new row was written. The unique key makes this safe when two
    transactions decide the same thing at once: exactly one of them inserts.
    """
    if kind not in NOTIFICATION_KINDS:
        raise ValueError("Unknown notification kind")
    values: dict[str, Any] = {
        "user_id": user_id,
        "kind": kind,
        "dedupe_key": dedupe_key,
        "payload": payload,
        "status": status,
        "last_error_code": reason,
    }
    if not_before is not None:
        values["next_attempt_at"] = not_before
    dialect = session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    statement = (
        insert(NotificationOutbox)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(NotificationOutbox.id)
    )
    return session.execute(statement).scalar_one_or_none() is not None

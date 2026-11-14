"""Read-only, parameterized queries over the append-only security event store."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, Date, and_, cast, func, literal_column, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.policy import (
    ACCOUNT_TARGET_TYPE,
    AuditCursor,
    AuditFilters,
    AuditPeriod,
    as_utc,
)
from finance_ai.db.models.audit import SecurityAuditEvent

Event = SecurityAuditEvent

# Static SQL fragments per supported reporting zone (never built from request input).
_POSTGRES_ZONE_LITERALS: Final[dict[str, str]] = {
    "America/Bogota": "'America/Bogota'",
    "UTC": "'UTC'",
}
_SQLITE_OFFSET_LITERALS: Final[dict[str, str]] = {
    "America/Bogota": "'-300 minutes'",
    "UTC": "'+0 minutes'",
}


@contextmanager
def snapshot_session(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One read-only transaction whose queries all observe the same database snapshot."""
    with factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield session
        finally:
            session.rollback()


def scope_conditions(
    period: AuditPeriod, filters: AuditFilters, read_boundary: datetime
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        Event.occurred_at >= period.start_utc,
        Event.occurred_at < period.end_utc,
        Event.occurred_at <= read_boundary,
    ]
    if filters.event_type is not None:
        conditions.append(Event.event_type == filters.event_type)
    if filters.outcome is not None:
        conditions.append(Event.result == filters.outcome)
    if filters.actor_id is not None:
        conditions.append(Event.actor_id == filters.actor_id)
    if filters.target_type is not None:
        conditions.append(Event.target_resource_type == filters.target_type)
    if filters.target_id is not None:
        conditions.append(Event.target_resource_id == filters.target_id)
    if filters.correlation_id is not None:
        conditions.append(Event.correlation_id == filters.correlation_id)
    return conditions


def account_history_condition(account_id: str) -> ColumnElement[bool]:
    """Actor OR typed affected-account target; each row is one unique event ID."""
    return or_(
        Event.actor_id == account_id,
        and_(
            Event.target_resource_type == ACCOUNT_TARGET_TYPE,
            Event.target_resource_id == account_id,
        ),
    )


def page(
    session: Session,
    conditions: Sequence[ColumnElement[bool]],
    *,
    cursor: AuditCursor | None,
    limit: int,
    newest_first: bool,
) -> list[SecurityAuditEvent]:
    statement = select(Event).where(*conditions)
    if cursor is not None:
        if newest_first:
            statement = statement.where(
                or_(
                    Event.occurred_at < cursor.occurred_at,
                    and_(
                        Event.occurred_at == cursor.occurred_at,
                        Event.event_id < cursor.event_id,
                    ),
                )
            )
        else:
            statement = statement.where(
                or_(
                    Event.occurred_at > cursor.occurred_at,
                    and_(
                        Event.occurred_at == cursor.occurred_at,
                        Event.event_id > cursor.event_id,
                    ),
                )
            )
    ordering = (
        (Event.occurred_at.desc(), Event.event_id.desc())
        if newest_first
        else (Event.occurred_at.asc(), Event.event_id.asc())
    )
    return list(session.scalars(statement.order_by(*ordering).limit(limit + 1)))


def all_in_scope(
    session: Session, conditions: Sequence[ColumnElement[bool]], *, limit: int
) -> list[SecurityAuditEvent]:
    return list(
        session.scalars(
            select(Event)
            .where(*conditions)
            .order_by(Event.occurred_at.asc(), Event.event_id.asc())
            .limit(limit)
        )
    )


def get_event(session: Session, event_id: object) -> SecurityAuditEvent | None:
    return session.get(Event, event_id)


def count_in_scope(session: Session, conditions: Sequence[ColumnElement[bool]]) -> int:
    return int(session.scalar(select(func.count()).select_from(Event).where(*conditions)) or 0)


def counts_by_type_outcome(
    session: Session, conditions: Sequence[ColumnElement[bool]]
) -> dict[tuple[str, str], int]:
    rows = session.execute(
        select(Event.event_type, Event.result, func.count())
        .where(*conditions)
        .group_by(Event.event_type, Event.result)
    ).all()
    return {(event_type, result): int(count) for event_type, result, count in rows}


def counts_by_local_day(
    session: Session, conditions: Sequence[ColumnElement[bool]], time_zone: str
) -> dict[date, int]:
    dialect = session.get_bind().dialect.name
    day: ColumnElement[Any]
    if dialect == "postgresql":
        day = cast(
            func.timezone(literal_column(_POSTGRES_ZONE_LITERALS[time_zone]), Event.occurred_at),
            Date,
        )
    else:
        day = func.date(Event.occurred_at, literal_column(_SQLITE_OFFSET_LITERALS[time_zone]))
    rows = session.execute(
        select(day.label("day"), func.count()).where(*conditions).group_by(day)
    ).all()
    result: dict[date, int] = {}
    for value, count in rows:
        bucket = value if isinstance(value, date) else date.fromisoformat(str(value))
        result[bucket] = result.get(bucket, 0) + int(count)
    return result


def earliest_recorded_at(session: Session) -> datetime | None:
    value = session.scalar(select(func.min(Event.occurred_at)))
    if value is None:
        return None
    if isinstance(value, str):  # SQLite aggregates return the stored text
        return as_utc(datetime.fromisoformat(value))
    return as_utc(value)

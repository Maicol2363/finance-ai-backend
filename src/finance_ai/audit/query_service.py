"""AUD-01 list/detail, AUD-02 investigation and AUD-03 summary read services."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit import repository
from finance_ai.audit.policy import (
    ACCOUNT_TARGET_TYPE,
    AUDIT_OUTCOMES,
    AUDIT_TARGET_TYPES,
    DEFAULT_TIME_ZONE,
    KNOWN_EVENT_TYPES,
    MAX_PAGE_SIZE,
    REPORTING_TIME_ZONES,
    SUMMARY_METRICS,
    TIME_ZONE_LABELS,
    AuditCursor,
    AuditFilters,
    AuditPeriod,
    MetricDefinition,
    as_utc,
    decode_cursor,
    encode_cursor,
    project_context,
    scope_fingerprint,
)
from finance_ai.audit.schemas import (
    AuditCatalogView,
    AuditDayCountView,
    AuditEventPage,
    AuditEventView,
    AuditFiltersView,
    AuditInvestigationEventView,
    AuditInvestigationPage,
    AuditMetricDefinitionView,
    AuditMetricMappingView,
    AuditMetricView,
    AuditPeriodView,
    AuditSummaryView,
    AuditTimeZoneView,
    AuditTypeCountView,
)
from finance_ai.auth.session_policy import Clock
from finance_ai.core.errors import AppError, AuditEventNotFoundError, AuditQueryUnavailableError
from finance_ai.db.models.audit import SecurityAuditEvent


class AuditQueryService:
    """Read-only Security Auditor queries; authorization happens before every call."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        *,
        max_query_days: int,
        max_report_events: int,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._max_query_days = max_query_days
        self._max_report_events = max_report_events

    def catalog(self) -> AuditCatalogView:
        return AuditCatalogView(
            event_types=list(KNOWN_EVENT_TYPES),
            outcomes=list(AUDIT_OUTCOMES),
            target_types=list(AUDIT_TARGET_TYPES),
            time_zones=[
                AuditTimeZoneView(name=name, label=TIME_ZONE_LABELS[name])
                for name in REPORTING_TIME_ZONES
            ],
            default_time_zone=DEFAULT_TIME_ZONE,
            max_query_days=self._max_query_days,
            max_report_events=self._max_report_events,
            max_page_size=MAX_PAGE_SIZE,
            metrics=[metric_definition_view(metric) for metric in SUMMARY_METRICS],
        )

    def list_events(
        self,
        period: AuditPeriod,
        filters: AuditFilters,
        *,
        cursor: str | None,
        limit: int,
    ) -> AuditEventPage:
        scope = scope_fingerprint("events", period, filters, "")
        position = decode_cursor(cursor, scope) if cursor is not None else None
        read_boundary = position.read_boundary if position is not None else self._clock.now()
        with self._reading() as session:
            rows = repository.page(
                session,
                repository.scope_conditions(period, filters, read_boundary),
                cursor=position,
                limit=limit,
                newest_first=True,
            )
        visible, next_cursor = _split_page(rows, limit, read_boundary, scope)
        return AuditEventPage(
            items=[event_view(row) for row in visible],
            next_cursor=next_cursor,
            read_boundary=read_boundary,
            period=period_view(period),
            filters=filters_view(filters),
        )

    def get_event(self, event_id: UUID) -> AuditEventView:
        with self._reading() as session:
            row = repository.get_event(session, event_id)
            view = None if row is None else event_view(row)
        if view is None:
            raise AuditEventNotFoundError()
        return view

    def investigate_account(
        self,
        account_id: str,
        period: AuditPeriod,
        *,
        cursor: str | None,
        limit: int,
    ) -> AuditInvestigationPage:
        no_filters = AuditFilters()
        scope = scope_fingerprint("investigation", period, no_filters, account_id)
        position = decode_cursor(cursor, scope) if cursor is not None else None
        read_boundary = position.read_boundary if position is not None else self._clock.now()
        conditions = [
            *repository.scope_conditions(period, no_filters, read_boundary),
            repository.account_history_condition(account_id),
        ]
        with self._reading() as session:
            rows = repository.page(
                session, conditions, cursor=position, limit=limit, newest_first=False
            )
        visible, next_cursor = _split_page(rows, limit, read_boundary, scope)
        return AuditInvestigationPage(
            account_id=account_id,
            items=[
                AuditInvestigationEventView(
                    **event_view(row).model_dump(), match=_match(row, account_id)
                )
                for row in visible
            ],
            next_cursor=next_cursor,
            read_boundary=read_boundary,
            period=period_view(period),
        )

    def summary(self, period: AuditPeriod, filters: AuditFilters) -> AuditSummaryView:
        try:
            with repository.snapshot_session(self._session_factory) as session:
                cutoff = self._clock.now()
                conditions = repository.scope_conditions(period, filters, cutoff)
                total = repository.count_in_scope(session, conditions)
                by_type_outcome = repository.counts_by_type_outcome(session, conditions)
                by_day = repository.counts_by_local_day(session, conditions, period.time_zone)
                earliest = repository.earliest_recorded_at(session)
        except SQLAlchemyError as exc:
            raise AuditQueryUnavailableError() from exc
        return AuditSummaryView(
            period=period_view(period),
            filters=filters_view(filters),
            cutoff=cutoff,
            total_events=total,
            metrics=[
                AuditMetricView(
                    **metric_definition_view(metric).model_dump(),
                    count=metric.count(by_type_outcome),
                )
                for metric in SUMMARY_METRICS
            ],
            by_type=type_count_views(by_type_outcome),
            by_day=[AuditDayCountView(date=day, count=by_day.get(day, 0)) for day in period.days()],
            earliest_recorded_at=earliest,
            coverage=coverage(period, earliest),
        )

    @contextmanager
    def _reading(self) -> Iterator[Session]:
        try:
            with self._session_factory() as session:
                yield session
        except AppError:
            raise
        except SQLAlchemyError as exc:
            raise AuditQueryUnavailableError() from exc


def _split_page(
    rows: list[SecurityAuditEvent], limit: int, read_boundary: datetime, scope: str
) -> tuple[list[SecurityAuditEvent], str | None]:
    if len(rows) <= limit:
        return rows, None
    visible = rows[:limit]
    last = visible[-1]
    cursor = AuditCursor(
        occurred_at=as_utc(last.occurred_at),
        event_id=last.event_id,
        read_boundary=read_boundary,
    )
    return visible, encode_cursor(cursor, scope)


def _match(
    row: SecurityAuditEvent, account_id: str
) -> Literal["actor", "target", "actor_and_target"]:
    is_actor = row.actor_id == account_id
    is_target = (
        row.target_resource_type == ACCOUNT_TARGET_TYPE and row.target_resource_id == account_id
    )
    if is_actor and is_target:
        return "actor_and_target"
    return "actor" if is_actor else "target"


def event_view(row: SecurityAuditEvent) -> AuditEventView:
    return AuditEventView(
        event_id=row.event_id,
        occurred_at=as_utc(row.occurred_at),
        event_type=row.event_type,
        outcome=row.result,
        actor_id=row.actor_id,
        target_type=row.target_resource_type,
        target_id=row.target_resource_id,
        correlation_id=row.correlation_id,
        context=project_context(row.context),
    )


def period_view(period: AuditPeriod) -> AuditPeriodView:
    return AuditPeriodView(
        start_date=period.start_date,
        end_date=period.end_date,
        time_zone=period.time_zone,
        time_zone_label=TIME_ZONE_LABELS[period.time_zone],
        start_utc=period.start_utc,
        end_utc=period.end_utc,
    )


def filters_view(filters: AuditFilters) -> AuditFiltersView:
    return AuditFiltersView(**filters.as_dict())


def metric_definition_view(metric: MetricDefinition) -> AuditMetricDefinitionView:
    return AuditMetricDefinitionView(
        key=metric.key,
        label=metric.label,
        description=metric.description,
        mapping=[
            AuditMetricMappingView(event_type=event_type, outcomes=list(outcomes))
            for event_type, outcomes in metric.mapping
        ],
    )


def type_count_views(by_type_outcome: dict[tuple[str, str], int]) -> list[AuditTypeCountView]:
    return [
        AuditTypeCountView(event_type=event_type, outcome=outcome, count=count)
        for (event_type, outcome), count in sorted(
            by_type_outcome.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
        )
    ]


def coverage(
    period: AuditPeriod, earliest: datetime | None
) -> Literal["available", "partial", "no_records"]:
    if earliest is None:
        return "no_records"
    return "partial" if period.start_utc < earliest else "available"

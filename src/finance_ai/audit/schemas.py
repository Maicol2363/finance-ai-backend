"""Sanitized Security Auditor API projections (AUD-01..AUD-04)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from finance_ai.audit.policy import (
    ACTOR_ID_PATTERN,
    CORRELATION_ID_PATTERN,
    DEFAULT_TIME_ZONE,
    EVENT_TYPE_PATTERN,
    TARGET_ID_PATTERN,
    AuditOutcome,
    AuditTargetType,
    TimeZoneName,
)


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditPeriodView(_View):
    start_date: date
    end_date: date = Field(description="Inclusive calendar end date in the reporting zone.")
    time_zone: str
    time_zone_label: str
    start_utc: datetime = Field(description="Inclusive UTC start.")
    end_utc: datetime = Field(description="Exclusive UTC end (midnight after end_date).")


class AuditFiltersView(_View):
    event_type: str | None = None
    outcome: str | None = None
    actor_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    correlation_id: str | None = None


class AuditEventView(_View):
    event_id: UUID
    occurred_at: datetime = Field(description="UTC timestamp recorded by the backend.")
    event_type: str
    outcome: str
    actor_id: str | None = Field(description="Historical actor identifier; null when unknown.")
    target_type: str | None
    target_id: str | None
    correlation_id: str | None
    context: dict[str, str] = Field(description="Allowlisted, sanitized event metadata.")


class AuditEventPage(_View):
    items: list[AuditEventView]
    next_cursor: str | None
    read_boundary: datetime = Field(
        description="Events recorded after this UTC instant are outside this traversal."
    )
    period: AuditPeriodView
    filters: AuditFiltersView


class AuditInvestigationEventView(AuditEventView):
    match: Literal["actor", "target", "actor_and_target"]


class AuditInvestigationPage(_View):
    account_id: str
    identity_lookup: Literal["not_performed"] = Field(
        default="not_performed",
        description="Recorded identifiers only; the current account directory is not read.",
    )
    items: list[AuditInvestigationEventView]
    next_cursor: str | None
    read_boundary: datetime
    period: AuditPeriodView


class AuditMetricMappingView(_View):
    event_type: str
    outcomes: list[str]


class AuditMetricDefinitionView(_View):
    key: str
    label: str
    description: str
    mapping: list[AuditMetricMappingView]


class AuditMetricView(AuditMetricDefinitionView):
    count: int


class AuditTypeCountView(_View):
    event_type: str
    outcome: str
    count: int


class AuditDayCountView(_View):
    date: date
    count: int


class AuditSummaryView(_View):
    period: AuditPeriodView
    filters: AuditFiltersView
    cutoff: datetime = Field(description="UTC instant of the summary snapshot.")
    total_events: int
    metrics: list[AuditMetricView]
    by_type: list[AuditTypeCountView]
    by_day: list[AuditDayCountView]
    earliest_recorded_at: datetime | None
    coverage: Literal["available", "partial", "no_records"] = Field(
        description=(
            "partial: the period starts before the oldest available record; no_records: the "
            "store holds no events. Neither is the same as zero activity."
        )
    )


class AuditTimeZoneView(_View):
    name: str
    label: str


class AuditCatalogView(_View):
    event_types: list[str]
    outcomes: list[str]
    target_types: list[str]
    time_zones: list[AuditTimeZoneView]
    default_time_zone: str
    max_query_days: int
    max_report_events: int
    max_page_size: int
    metrics: list[AuditMetricDefinitionView]


class AuditReportRequest(_View):
    start_date: date
    end_date: date
    time_zone: TimeZoneName = DEFAULT_TIME_ZONE
    event_type: str | None = Field(default=None, max_length=100, pattern=EVENT_TYPE_PATTERN)
    outcome: AuditOutcome | None = None
    actor_id: str | None = Field(default=None, pattern=ACTOR_ID_PATTERN)
    target_type: AuditTargetType | None = None
    target_id: str | None = Field(default=None, pattern=TARGET_ID_PATTERN)
    correlation_id: str | None = Field(default=None, pattern=CORRELATION_ID_PATTERN)

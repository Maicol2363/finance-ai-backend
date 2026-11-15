"""FastAPI wiring for the Security Auditor read and report services."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Query, Request
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from finance_ai.audit.policy import (
    ACTOR_ID_PATTERN,
    CORRELATION_ID_PATTERN,
    DEFAULT_TIME_ZONE,
    EVENT_TYPE_PATTERN,
    TARGET_ID_PATTERN,
    AuditFilters,
    AuditOutcome,
    AuditPeriod,
    AuditTargetType,
    TimeZoneName,
    normalize_period,
)
from finance_ai.audit.query_service import AuditQueryService
from finance_ai.audit.report_service import AuditReportService
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.session_policy import SystemClock
from finance_ai.core.config import get_settings
from finance_ai.core.errors import AuditRateLimitedError
from finance_ai.db.session import get_session_factory
from finance_ai.staff_auth.dependencies import get_security_auditor_user
from finance_ai.staff_auth.schemas import StaffSessionUser


def get_audit_query_service() -> AuditQueryService:
    settings = get_settings()
    return AuditQueryService(
        get_session_factory(),
        SystemClock(),
        max_query_days=settings.audit_max_query_days,
        max_report_events=settings.audit_max_report_events,
    )


def get_audit_report_service() -> AuditReportService:
    return AuditReportService(
        get_session_factory(),
        SecurityAuditWriter(),
        SystemClock(),
        max_events=get_settings().audit_max_report_events,
    )


@lru_cache
def _audit_limiter() -> FixedWindowRateLimiter:
    return FixedWindowRateLimiter(MemoryStorage())


def _peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce_audit_read_limit(
    request: Request,
    auditor: Annotated[StaffSessionUser, Depends(get_security_auditor_user)],
) -> StaffSessionUser:
    if not _audit_limiter().hit(parse("120/minute"), "audit-read", _peer(request)):
        raise AuditRateLimitedError()
    return auditor


def enforce_audit_report_limit(
    request: Request,
    auditor: Annotated[StaffSessionUser, Depends(get_security_auditor_user)],
) -> StaffSessionUser:
    if not _audit_limiter().hit(parse("6/minute"), "audit-report", str(auditor.id)) or not (
        _audit_limiter().hit(parse("120/minute"), "audit-read", _peer(request))
    ):
        raise AuditRateLimitedError()
    return auditor


def audit_period(
    start_date: Annotated[date, Query(description="Inclusive start date in the time zone.")],
    end_date: Annotated[date, Query(description="Inclusive end date in the time zone.")],
    time_zone: Annotated[TimeZoneName, Query()] = DEFAULT_TIME_ZONE,
) -> AuditPeriod:
    return normalize_period(
        start_date, end_date, time_zone, max_days=get_settings().audit_max_query_days
    )


def audit_filters(
    event_type: Annotated[str | None, Query(max_length=100, pattern=EVENT_TYPE_PATTERN)] = None,
    outcome: Annotated[AuditOutcome | None, Query()] = None,
    actor_id: Annotated[str | None, Query(pattern=ACTOR_ID_PATTERN)] = None,
    target_type: Annotated[AuditTargetType | None, Query()] = None,
    target_id: Annotated[str | None, Query(pattern=TARGET_ID_PATTERN)] = None,
    correlation_id: Annotated[str | None, Query(pattern=CORRELATION_ID_PATTERN)] = None,
) -> AuditFilters:
    return AuditFilters(
        event_type=event_type,
        outcome=outcome,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        correlation_id=correlation_id,
    )


Auditor = Annotated[StaffSessionUser, Depends(enforce_audit_read_limit)]
ReportAuditor = Annotated[StaffSessionUser, Depends(enforce_audit_report_limit)]
QueryService = Annotated[AuditQueryService, Depends(get_audit_query_service)]
ReportService = Annotated[AuditReportService, Depends(get_audit_report_service)]
Period = Annotated[AuditPeriod, Depends(audit_period)]
Filters = Annotated[AuditFilters, Depends(audit_filters)]

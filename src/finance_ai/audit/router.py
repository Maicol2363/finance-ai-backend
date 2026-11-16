"""Security Auditor API (AUD-01..AUD-04). Read-only: no update/delete of audit evidence."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from finance_ai.audit.dependencies import (
    Auditor,
    Filters,
    Period,
    QueryService,
    ReportAuditor,
    ReportService,
)
from finance_ai.audit.policy import (
    ACTOR_ID_PATTERN,
    CURSOR_PATTERN,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AuditFilters,
    normalize_period,
)
from finance_ai.audit.schemas import (
    AuditCatalogView,
    AuditEventPage,
    AuditEventView,
    AuditInvestigationPage,
    AuditReportRequest,
    AuditSummaryView,
)
from finance_ai.core.config import get_settings
from finance_ai.core.errors import ProblemResponse
from finance_ai.staff_auth.dependencies import (
    get_staff_session_service,
    require_staff_request_boundary,
    staff_cookie_names,
)
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.staff_auth.service import StaffSessionService

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ProblemResponse},
    403: {"model": ProblemResponse},
    422: {"model": ProblemResponse},
    429: {"model": ProblemResponse},
    503: {"model": ProblemResponse},
}

router = APIRouter(
    prefix="/staff/audit",
    tags=["Security Auditor"],
    dependencies=[Depends(require_staff_request_boundary)],
    responses=_ERRORS,
)

Cursor = Annotated[str | None, Query(pattern=CURSOR_PATTERN)]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


def _correlation(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "unavailable"))


@router.get("/catalog", response_model=AuditCatalogView, summary="Audit Filter Catalog")
def audit_catalog(auditor: Auditor, service: QueryService) -> AuditCatalogView:
    del auditor
    return service.catalog()


@router.get(
    "/events",
    response_model=AuditEventPage,
    summary="List Security Events",
    description=(
        "AUD-01: newest first by UTC timestamp then event ID. The period uses inclusive "
        "calendar dates in the reporting zone, normalized to an inclusive UTC start and an "
        "exclusive UTC end. Pages stay stable within the returned read boundary."
    ),
)
def list_security_events(
    auditor: Auditor,
    service: QueryService,
    period: Period,
    filters: Filters,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> AuditEventPage:
    del auditor
    return service.list_events(period, filters, cursor=cursor, limit=limit)


@router.get(
    "/events/{event_id}",
    response_model=AuditEventView,
    summary="Security Event Detail",
    responses={404: {"model": ProblemResponse}},
)
def get_security_event(auditor: Auditor, service: QueryService, event_id: UUID) -> AuditEventView:
    del auditor
    return service.get_event(event_id)


@router.get(
    "/investigations/{account_id}",
    response_model=AuditInvestigationPage,
    summary="Investigate Account Activity",
    description=(
        "AUD-02: events where the historical account identifier is the actor or the typed "
        "affected account target, deduplicated by event ID and ordered oldest first. The "
        "account need not exist or be active; the account directory is not read."
    ),
)
def investigate_account(
    auditor: Auditor,
    service: QueryService,
    period: Period,
    account_id: Annotated[str, Path(pattern=ACTOR_ID_PATTERN)],
    cursor: Cursor = None,
    limit: Limit = DEFAULT_PAGE_SIZE,
) -> AuditInvestigationPage:
    del auditor
    return service.investigate_account(account_id, period, cursor=cursor, limit=limit)


@router.get(
    "/summary",
    response_model=AuditSummaryView,
    summary="Security Activity Summary",
    description=(
        "AUD-03: backend aggregates over the complete authorized filtered dataset (never a "
        "page), with documented event-type/outcome mappings and daily buckets in the zone."
    ),
)
def security_summary(
    auditor: Auditor, service: QueryService, period: Period, filters: Filters
) -> AuditSummaryView:
    del auditor
    return service.summary(period, filters)


@router.post(
    "/reports",
    summary="Generate Security Audit Report",
    description=(
        "AUD-04: generates a PDF from one database snapshot, rejects selections above the "
        "configured maximum, rechecks the Auditor's authority at delivery and records "
        "audit.report_exported before returning the file."
    ),
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The generated PDF report."},
        500: {"model": ProblemResponse},
    },
)
def generate_security_report(
    payload: AuditReportRequest,
    request: Request,
    auditor: ReportAuditor,
    service: ReportService,
    staff_sessions: Annotated[StaffSessionService, Depends(get_staff_session_service)],
) -> Response:
    period = normalize_period(
        payload.start_date,
        payload.end_date,
        payload.time_zone,
        max_days=get_settings().audit_max_query_days,
    )
    filters = AuditFilters(
        event_type=payload.event_type,
        outcome=payload.outcome,
        actor_id=payload.actor_id,
        actor_role=payload.actor_role,
        target_type=payload.target_type,
        target_id=payload.target_id,
        correlation_id=payload.correlation_id,
    )
    access_cookie, _refresh_cookie = staff_cookie_names()

    def authorize_delivery() -> StaffSessionUser:
        # Re-resolves the session: suspension, role loss or missing MFA denies delivery.
        return staff_sessions.resolve(
            request.cookies.get(access_cookie), correlation_id=_correlation(request)
        )

    report = service.export(
        auditor=auditor,
        period=period,
        filters=filters,
        correlation_id=_correlation(request),
        authorize_delivery=authorize_delivery,
    )
    return Response(
        content=report.content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{report.filename}"',
            "X-Report-Id": str(report.report_id),
            "X-Report-Event-Count": str(report.event_count),
            "X-Content-Type-Options": "nosniff",
        },
    )

"""AUD-04 export: one snapshot, bounded size, authority recheck and mandatory export audit."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit import repository
from finance_ai.audit.policy import AuditFilters, AuditPeriod, as_utc, project_context
from finance_ai.audit.report import (
    ReportEvent,
    ReportSnapshot,
    compute_statistics,
    render_report_pdf,
)
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.session_policy import Clock
from finance_ai.core.errors import (
    AuditExportUnrecordedError,
    AuditQueryUnavailableError,
    AuditReportGenerationError,
    AuditReportTooLargeError,
    StaffAccessDeniedError,
)
from finance_ai.core.logging import get_logger
from finance_ai.core.roles import SECURITY_AUDITOR_ROLE
from finance_ai.staff_auth.schemas import StaffSessionUser

logger = get_logger(__name__)

PdfRenderer = Callable[[ReportSnapshot], bytes]

# PostgreSQL SQLSTATE raised by sp_audit_record_report_export when authority is gone.
_INSUFFICIENT_PRIVILEGE = "42501"
_RECORD_REPORT_EXPORT = text(
    "CALL sp_audit_record_report_export("
    ":auditor_id, :report_id, :correlation_id, :occurred_at, CAST(:context AS jsonb))"
)


class ExportEvidenceRecorder(Protocol):
    """Durably records a successful AUD-04 export; raises StaffAccessDeniedError if denied."""

    def record_export(
        self,
        *,
        auditor_id: int,
        report_id: UUID,
        correlation_id: str,
        occurred_at: datetime,
        context: dict[str, str],
    ) -> None: ...


class ProcedureExportRecorder:
    """Production recorder: ``sp_audit_record_report_export`` (migration 0022).

    In one transaction the procedure share-locks the auditor's account, re-checks that it is an
    active, authoritatively activated ``security_auditor`` and inserts ``audit.report_exported``,
    so a suspension or role change cannot slip between the delivery check and the evidence.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record_export(
        self,
        *,
        auditor_id: int,
        report_id: UUID,
        correlation_id: str,
        occurred_at: datetime,
        context: dict[str, str],
    ) -> None:
        SecurityAuditWriter.validate(
            event_type="audit.report_exported",
            result="success",
            context=context,
            target_type="audit_report",
        )
        try:
            with self._session_factory() as session, session.begin():
                session.execute(
                    _RECORD_REPORT_EXPORT,
                    {
                        "auditor_id": auditor_id,
                        "report_id": report_id,
                        "correlation_id": correlation_id,
                        "occurred_at": occurred_at,
                        "context": json.dumps(context),
                    },
                )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == _INSUFFICIENT_PRIVILEGE:
                raise StaffAccessDeniedError() from exc
            raise


class WriterExportRecorder:
    """Writer-based recorder for databases without the stored procedure (SQLite unit tests)."""

    def __init__(self, session_factory: sessionmaker[Session], writer: SecurityAuditWriter) -> None:
        self._session_factory = session_factory
        self._writer = writer

    def record_export(
        self,
        *,
        auditor_id: int,
        report_id: UUID,
        correlation_id: str,
        occurred_at: datetime,
        context: dict[str, str],
    ) -> None:
        del occurred_at  # The writer stamps its own UTC time.
        with self._session_factory() as session, session.begin():
            self._writer.append(
                session,
                event_type="audit.report_exported",
                actor_id=str(auditor_id),
                target_id=str(report_id),
                target_type="audit_report",
                result="success",
                correlation_id=correlation_id,
                context=context,
            )


@dataclass(frozen=True)
class GeneratedReport:
    report_id: UUID
    filename: str
    content: bytes
    event_count: int


class AuditReportService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        writer: SecurityAuditWriter,
        clock: Clock,
        *,
        max_events: int,
        renderer: PdfRenderer = render_report_pdf,
        export_recorder: ExportEvidenceRecorder | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._writer = writer
        self._clock = clock
        self._max_events = max_events
        self._renderer = renderer
        # Production wiring passes ProcedureExportRecorder (see audit.dependencies).
        self._export_recorder = export_recorder or WriterExportRecorder(session_factory, writer)

    def export(
        self,
        *,
        auditor: StaffSessionUser,
        period: AuditPeriod,
        filters: AuditFilters,
        correlation_id: str,
        authorize_delivery: Callable[[], StaffSessionUser],
    ) -> GeneratedReport:
        report_id = uuid4()
        snapshot = self._read_snapshot(report_id, auditor, period, filters)
        try:
            content = self._renderer(snapshot)
        except Exception as exc:
            self._record_failure(snapshot, correlation_id, reason="generation_failed")
            raise AuditReportGenerationError() from exc

        # Authority is rechecked at delivery: a suspension or role removal that happened
        # while the report was being generated blocks the download.
        current = authorize_delivery()
        if current.id != auditor.id or current.role != SECURITY_AUDITOR_ROLE:
            raise StaffAccessDeniedError()

        # The export event is written after (and outside) the snapshot it describes, and
        # durable evidence must exist before any successful PDF is returned.
        try:
            self._export_recorder.record_export(
                auditor_id=auditor.id,
                report_id=report_id,
                correlation_id=correlation_id,
                occurred_at=self._clock.now(),
                context=_export_context(snapshot),
            )
        except StaffAccessDeniedError:
            raise
        except Exception as exc:
            logger.error(
                "Report delivery blocked because export evidence was not recorded",
                extra={
                    "correlation_id": correlation_id,
                    "event": "audit.report_export_unrecorded",
                    "error_type": type(exc).__name__,
                    "outcome": "blocked",
                },
            )
            raise AuditExportUnrecordedError() from exc
        return GeneratedReport(
            report_id=report_id,
            filename=f"informe-auditoria-{report_id}.pdf",
            content=content,
            event_count=snapshot.statistics.total,
        )

    def _read_snapshot(
        self,
        report_id: UUID,
        auditor: StaffSessionUser,
        period: AuditPeriod,
        filters: AuditFilters,
    ) -> ReportSnapshot:
        try:
            with repository.snapshot_session(self._session_factory) as session:
                cutoff = self._clock.now()
                conditions = repository.scope_conditions(period, filters, cutoff)
                matching = repository.count_in_scope(session, conditions)
                if matching > self._max_events:
                    raise AuditReportTooLargeError(
                        matching_events=matching, max_events=self._max_events
                    )
                rows = repository.all_in_scope(session, conditions, limit=self._max_events)
                events = tuple(
                    ReportEvent(
                        event_id=row.event_id,
                        occurred_at=as_utc(row.occurred_at),
                        event_type=row.event_type,
                        outcome=row.result,
                        actor_id=row.actor_id,
                        actor_role=row.actor_role,
                        target_type=row.target_resource_type,
                        target_id=row.target_resource_id,
                        correlation_id=row.correlation_id,
                        context=project_context(row.context),
                    )
                    for row in rows
                )
        except SQLAlchemyError as exc:
            raise AuditQueryUnavailableError() from exc
        return ReportSnapshot(
            report_id=report_id,
            period=period,
            filters=filters,
            cutoff=cutoff,
            generated_at=self._clock.now(),
            acting_auditor_id=auditor.id,
            events=events,
            statistics=compute_statistics(events, period),
        )

    def _record_failure(
        self, snapshot: ReportSnapshot, correlation_id: str, *, reason: str
    ) -> None:
        context = _export_context(snapshot)
        context["reason"] = reason
        try:
            with self._session_factory() as session, session.begin():
                self._writer.append(
                    session,
                    event_type="audit.report_exported",
                    actor_id=str(snapshot.acting_auditor_id),
                    target_id=str(snapshot.report_id),
                    target_type="audit_report",
                    result="failure",
                    correlation_id=correlation_id,
                    context=context,
                )
        except Exception as exc:
            logger.warning(
                "Failed report attempt could not be recorded",
                extra={
                    "correlation_id": correlation_id,
                    "event": "audit.report_failure_unrecorded",
                    "error_type": type(exc).__name__,
                    "outcome": "failure",
                },
            )


def _export_context(snapshot: ReportSnapshot) -> dict[str, str]:
    return {
        "report_id": str(snapshot.report_id),
        "period_start": snapshot.period.start_date.isoformat(),
        "period_end": snapshot.period.end_date.isoformat(),
        "time_zone": snapshot.period.time_zone,
        "event_count": str(snapshot.statistics.total),
        **snapshot.filters.as_export_context(),
    }

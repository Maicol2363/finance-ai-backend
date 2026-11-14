"""AUD-04 snapshot, limits, PDF content and mandatory export-audit failure safety."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from reportlab import rl_config
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.policy import AuditFilters, normalize_period
from finance_ai.audit.report import (
    ReportSnapshot,
    compute_statistics,
    observations,
    render_report_pdf,
)
from finance_ai.audit.report_service import AuditReportService
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.core.errors import (
    AuditExportUnrecordedError,
    AuditReportGenerationError,
    AuditReportTooLargeError,
    StaffAccessDeniedError,
)
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.staff_auth.schemas import StaffSessionUser
from tests.audit_support import FixedClock, add_event

NOW = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)
AUDITOR = StaffSessionUser(id=5, email="auditor@example.test", role="security_auditor")
PERIOD = normalize_period(date(2026, 10, 1), date(2026, 10, 14), "America/Bogota", max_days=90)


@pytest.fixture
def uncompressed_pdf() -> Iterator[None]:
    previous = rl_config.pageCompression
    rl_config.pageCompression = 0
    yield
    rl_config.pageCompression = previous


def make_service(
    factory: sessionmaker[Session],
    *,
    max_events: int = 2_000,
    writer: SecurityAuditWriter | None = None,
    renderer: object = None,
) -> AuditReportService:
    kwargs: dict[str, object] = {}
    if renderer is not None:
        kwargs["renderer"] = renderer
    return AuditReportService(
        factory,
        writer or SecurityAuditWriter(),
        FixedClock(NOW),
        max_events=max_events,
        **kwargs,  # type: ignore[arg-type]
    )


def export(service: AuditReportService, filters: AuditFilters | None = None) -> object:
    return service.export(
        auditor=AUDITOR,
        period=PERIOD,
        filters=filters or AuditFilters(),
        correlation_id="report-test",
        authorize_delivery=lambda: AUDITOR,
    )


def seed(factory: sessionmaker[Session], count: int) -> None:
    for index in range(count):
        add_event(
            factory,
            occurred_at=datetime(2026, 10, 3, 12, tzinfo=UTC) + timedelta(minutes=index),
            event_type="auth.login_success" if index % 2 else "access.denied",
            result="success" if index % 2 else "denied",
            context={"reason": "role_not_permitted", "password": "must-not-print"},
        )


def export_events(factory: sessionmaker[Session]) -> list[SecurityAuditEvent]:
    with factory() as session:
        return list(
            session.scalars(
                select(SecurityAuditEvent).where(
                    SecurityAuditEvent.event_type == "audit.report_exported"
                )
            )
        )


def test_export_records_evidence_after_the_snapshot_without_counting_itself(
    audit_factory: sessionmaker[Session],
) -> None:
    seed(audit_factory, 4)
    service = make_service(audit_factory)
    report = export(service, AuditFilters(outcome="denied"))

    assert report.content.startswith(b"%PDF")  # type: ignore[attr-defined]
    assert report.event_count == 2  # type: ignore[attr-defined]
    [recorded] = export_events(audit_factory)
    assert recorded.actor_id == str(AUDITOR.id)
    assert recorded.result == "success"
    assert recorded.target_resource_type == "audit_report"
    assert recorded.target_resource_id == str(report.report_id)  # type: ignore[attr-defined]
    assert recorded.context == {
        "report_id": str(report.report_id),  # type: ignore[attr-defined]
        "period_start": "2026-10-01",
        "period_end": "2026-10-14",
        "time_zone": "America/Bogota",
        "event_count": "2",
        "filter_outcome": "denied",
    }
    later = make_service(audit_factory)
    assert export(later).event_count == 4  # type: ignore[attr-defined]
    # The export event was written after NOW's snapshot cutoff, so it is outside both reports.
    assert len(export_events(audit_factory)) == 2


def test_configured_maximum_is_inclusive_and_never_truncates(
    audit_factory: sessionmaker[Session],
) -> None:
    seed(audit_factory, 3)
    assert export(make_service(audit_factory, max_events=3)).event_count == 3  # type: ignore[attr-defined]
    add_event(audit_factory, occurred_at=datetime(2026, 10, 4, tzinfo=UTC))
    with pytest.raises(AuditReportTooLargeError) as rejected:
        export(make_service(audit_factory, max_events=3))
    assert "4 events" in rejected.value.detail and "at most 3" in rejected.value.detail
    assert len(export_events(audit_factory)) == 1


def test_zero_event_period_produces_an_accurate_empty_report(
    audit_factory: sessionmaker[Session],
) -> None:
    report = export(make_service(audit_factory))
    assert report.event_count == 0  # type: ignore[attr-defined]
    assert report.content.startswith(b"%PDF")  # type: ignore[attr-defined]
    assert export_events(audit_factory)[0].context["event_count"] == "0"


def test_audit_write_failure_blocks_delivery(audit_factory: sessionmaker[Session]) -> None:
    seed(audit_factory, 2)

    class FailingWriter(SecurityAuditWriter):
        def append(self, session: Session, **kwargs: object) -> None:  # type: ignore[override]
            raise RuntimeError("audit store unavailable")

    with pytest.raises(AuditExportUnrecordedError) as blocked:
        export(make_service(audit_factory, writer=FailingWriter()))
    assert blocked.value.status == 503
    assert export_events(audit_factory) == []


def test_generation_failure_records_failure_and_returns_no_file(
    audit_factory: sessionmaker[Session],
) -> None:
    seed(audit_factory, 1)

    def broken_renderer(snapshot: ReportSnapshot) -> bytes:
        raise RuntimeError("layout failure")

    with pytest.raises(AuditReportGenerationError):
        export(make_service(audit_factory, renderer=broken_renderer))
    [failure] = export_events(audit_factory)
    assert failure.result == "failure"
    assert failure.context["reason"] == "generation_failed"


def test_authority_is_rechecked_before_delivery(audit_factory: sessionmaker[Session]) -> None:
    seed(audit_factory, 1)
    service = make_service(audit_factory)

    def revoked() -> StaffSessionUser:
        raise StaffAccessDeniedError()

    with pytest.raises(StaffAccessDeniedError):
        service.export(
            auditor=AUDITOR,
            period=PERIOD,
            filters=AuditFilters(),
            correlation_id="revoked",
            authorize_delivery=revoked,
        )
    demoted = StaffSessionUser(id=AUDITOR.id, email=AUDITOR.email, role="support_agent")
    with pytest.raises(StaffAccessDeniedError):
        service.export(
            auditor=AUDITOR,
            period=PERIOD,
            filters=AuditFilters(),
            correlation_id="demoted",
            authorize_delivery=lambda: demoted,
        )
    assert export_events(audit_factory) == []


def test_statistics_and_observations_are_deterministic() -> None:
    assert observations(compute_statistics((), PERIOD)) == [
        "No hay eventos registrados que coincidan con el alcance seleccionado."
    ]


def test_pdf_layout_contains_metadata_sections_and_every_row_across_pages(
    audit_factory: sessionmaker[Session], uncompressed_pdf: None
) -> None:
    seed(audit_factory, 120)
    captured: list[ReportSnapshot] = []

    def capture(snapshot: ReportSnapshot) -> bytes:
        captured.append(snapshot)
        return render_report_pdf(snapshot)

    report = export(make_service(audit_factory, renderer=capture))
    pdf = report.content  # type: ignore[attr-defined]
    snapshot = captured[0]
    text = pdf.decode("latin-1")

    assert len(re.findall(r"/Type /Page\b", text)) >= 4
    for expected in (
        "Informe de auditor",
        "Resumen ejecutivo",
        "Estad",
        "Observaciones factuales",
        "Reglas deterministas aplicadas",
        "Distribuci",
        "Actividad por d",
        "Tabla cronol",
        str(snapshot.report_id),
        "America/Bogota \\(UTC-05:00\\)",
        "120 eventos incluidos",
    ):
        assert expected in text, expected
    assert str(snapshot.events[0].event_id) in text
    assert str(snapshot.events[-1].event_id) in text
    assert "must-not-print" not in text and "password" not in text
    assert snapshot.statistics.total == len(snapshot.events) == 120
    assert sum(count for _, count in snapshot.statistics.by_day) == 120
    assert [event.occurred_at for event in snapshot.events] == sorted(
        event.occurred_at for event in snapshot.events
    )


def test_maximum_size_report_renders(audit_factory: sessionmaker[Session]) -> None:
    seed(audit_factory, 2_000)
    report = export(make_service(audit_factory))
    assert report.event_count == 2_000  # type: ignore[attr-defined]
    assert report.content.startswith(b"%PDF")  # type: ignore[attr-defined]
    add_event(audit_factory, occurred_at=datetime(2026, 10, 13, tzinfo=UTC))
    with pytest.raises(AuditReportTooLargeError):
        export(make_service(audit_factory))

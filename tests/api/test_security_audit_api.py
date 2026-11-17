"""Security Auditor HTTP boundary: role isolation, validation, privacy and PDF delivery."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit import dependencies as audit_dependencies
from finance_ai.audit.dependencies import get_audit_query_service, get_audit_report_service
from finance_ai.audit.query_service import AuditQueryService
from finance_ai.audit.report_service import AuditReportService
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.core.config import get_settings
from finance_ai.core.errors import StaffAccessDeniedError, StaffAuthenticationRequiredError
from finance_ai.db.models.audit import SecurityAuditEvent
from finance_ai.main import create_app
from finance_ai.staff_auth.dependencies import (
    get_access_denial_recorder,
    get_staff_session_service,
    staff_cookie_names,
)
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.dependencies import get_support_ticket_service
from tests.audit_support import FixedClock, add_event

ORIGIN = "https://staff.example.test"
NOW = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)
GET_HEADERS = {"X-Staff-Request": "1", "Sec-Fetch-Site": "same-origin"}
POST_HEADERS = {"X-Staff-Request": "1", "Origin": ORIGIN}
PERIOD = {"start_date": "2026-10-01", "end_date": "2026-10-14"}
ROLES = {
    "auditor-access": StaffSessionUser(id=5, email="auditor@example.test", role="security_auditor"),
    "agent-access": StaffSessionUser(id=6, email="agent@example.test", role="support_agent"),
    # Resolved as a fully authorized administrator (active, MFA-verified): the role alone denies.
    "admin-access": StaffSessionUser(id=7, email="admin@example.test", role="admin"),
}


@dataclass
class StubSessions:
    revoked: set[str] = field(default_factory=set)
    resolutions: int = 0

    def resolve(self, raw_access: str | None, *, correlation_id: str) -> StaffSessionUser:
        del correlation_id
        self.resolutions += 1
        if raw_access in self.revoked:
            raise StaffAccessDeniedError()
        if raw_access not in ROLES:
            raise StaffAuthenticationRequiredError()
        return ROLES[raw_access]


@dataclass
class RecordingDenials:
    calls: list[dict[str, object]] = field(default_factory=list)

    def record(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


@dataclass
class Harness:
    client: TestClient
    sessions: StubSessions
    denials: RecordingDenials
    factory: sessionmaker[Session]


@pytest.fixture
def harness(
    monkeypatch: pytest.MonkeyPatch, audit_factory: sessionmaker[Session]
) -> Iterator[Harness]:
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", f'["{ORIGIN}"]')
    get_settings.cache_clear()
    audit_dependencies._audit_limiter.cache_clear()
    app = create_app()
    sessions = StubSessions()
    denials = RecordingDenials()
    app.dependency_overrides[get_staff_session_service] = lambda: sessions
    app.dependency_overrides[get_access_denial_recorder] = lambda: denials
    app.dependency_overrides[get_audit_query_service] = lambda: AuditQueryService(
        audit_factory, FixedClock(NOW), max_query_days=90, max_report_events=2_000
    )
    app.dependency_overrides[get_audit_report_service] = lambda: AuditReportService(
        audit_factory, SecurityAuditWriter(), FixedClock(NOW), max_events=2_000
    )
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        yield Harness(client, sessions, denials, audit_factory)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def as_role(harness: Harness, access: str) -> None:
    harness.client.cookies.set(staff_cookie_names()[0], access)


def test_active_auditor_reads_sanitized_events_and_details(harness: Harness) -> None:
    event_id = add_event(
        harness.factory,
        occurred_at=datetime(2026, 10, 5, 12, tzinfo=UTC),
        event_type="auth.mfa_failed",
        result="failure",
        context={"reason": "invalid_code", "secret": "must-not-leak"},
    )
    as_role(harness, "auditor-access")
    page = harness.client.get("/api/v1/staff/audit/events", params=PERIOD, headers=GET_HEADERS)
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"].startswith("no-store")
    body = page.json()
    assert body["items"][0]["event_id"] == str(event_id)
    assert body["items"][0]["context"] == {"reason": "invalid_code"}
    assert body["period"]["start_utc"] == "2026-10-01T05:00:00Z"
    assert body["period"]["end_utc"] == "2026-10-15T05:00:00Z"
    assert "must-not-leak" not in page.text

    detail = harness.client.get(f"/api/v1/staff/audit/events/{event_id}", headers=GET_HEADERS)
    assert detail.status_code == 200 and detail.json()["outcome"] == "failure"
    missing = harness.client.get(
        "/api/v1/staff/audit/events/00000000-0000-0000-0000-000000000001", headers=GET_HEADERS
    )
    assert missing.status_code == 404 and missing.json()["code"] == "audit_event_not_found"


@pytest.mark.parametrize(
    ("access", "actor_id"),
    [("agent-access", 6), ("admin-access", 7)],
    ids=["support_agent", "admin"],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/staff/audit/catalog"),
        ("get", "/api/v1/staff/audit/events"),
        ("get", "/api/v1/staff/audit/events/00000000-0000-0000-0000-000000000001"),
        ("get", "/api/v1/staff/audit/investigations/15"),
        ("get", "/api/v1/staff/audit/summary"),
        ("post", "/api/v1/staff/audit/reports"),
    ],
)
def test_other_staff_roles_cannot_use_any_auditor_function(
    harness: Harness, access: str, actor_id: int, method: str, path: str
) -> None:
    as_role(harness, access)
    if method == "get":
        response = harness.client.get(path, params=PERIOD, headers=GET_HEADERS)
    else:
        response = harness.client.post(path, json=PERIOD, headers=POST_HEADERS)
    assert response.status_code == 403
    assert response.json()["code"] == "staff_access_denied"
    assert len(harness.denials.calls) == 1
    assert harness.denials.calls[-1]["resource"] == "security_audit"
    assert harness.denials.calls[-1]["reason"] == "role_not_permitted"
    assert harness.denials.calls[-1]["actor_id"] == actor_id
    with harness.factory() as session:
        assert session.scalar(select(SecurityAuditEvent.event_id)) is None


def test_anonymous_revoked_and_cross_site_requests_are_rejected(harness: Harness) -> None:
    anonymous = harness.client.get("/api/v1/staff/audit/events", params=PERIOD, headers=GET_HEADERS)
    assert anonymous.status_code == 401

    as_role(harness, "auditor-access")
    no_proof = harness.client.get("/api/v1/staff/audit/events", params=PERIOD)
    assert no_proof.status_code == 403 and no_proof.json()["code"] == "staff_request_denied"
    cross_site = harness.client.get(
        "/api/v1/staff/audit/events",
        params=PERIOD,
        headers={"X-Staff-Request": "1", "Sec-Fetch-Site": "cross-site"},
    )
    assert cross_site.status_code == 403

    harness.sessions.revoked.add("auditor-access")
    revoked = harness.client.get("/api/v1/staff/audit/events", params=PERIOD, headers=GET_HEADERS)
    assert revoked.status_code == 403


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"start_date": "2026-10-14", "end_date": "2026-10-01"}, "audit_query_invalid"),
        ({"start_date": "2026-01-01", "end_date": "2026-10-01"}, "audit_period_too_long"),
        ({**PERIOD, "event_type": "DROP TABLE"}, "request_invalid"),
        ({**PERIOD, "actor_id": "abc"}, "request_invalid"),
        ({**PERIOD, "outcome": "maybe"}, "request_invalid"),
        ({**PERIOD, "target_type": "app_user"}, "request_invalid"),
        ({**PERIOD, "target_id": "15"}, "audit_query_invalid"),
        ({**PERIOD, "limit": "101"}, "request_invalid"),
        ({**PERIOD, "cursor": "bad cursor"}, "request_invalid"),
        ({**PERIOD, "cursor": "e30"}, "audit_query_invalid"),
        ({"start_date": "2026-10-01"}, "request_invalid"),
        ({**PERIOD, "time_zone": "Europe/Madrid"}, "request_invalid"),
    ],
)
def test_invalid_queries_receive_safe_actionable_errors(
    harness: Harness, params: dict[str, str], code: str
) -> None:
    as_role(harness, "auditor-access")
    response = harness.client.get("/api/v1/staff/audit/events", params=params, headers=GET_HEADERS)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == code and body["correlation_id"]
    assert "Traceback" not in response.text and "SELECT" not in response.text


def test_investigation_and_summary_endpoints(harness: Harness) -> None:
    add_event(harness.factory, occurred_at=datetime(2026, 10, 5, tzinfo=UTC), actor_id="15")
    add_event(
        harness.factory,
        occurred_at=datetime(2026, 10, 6, tzinfo=UTC),
        actor_id="1",
        target_type="account",
        target_id="15",
        event_type="role.changed",
    )
    as_role(harness, "auditor-access")
    investigation = harness.client.get(
        "/api/v1/staff/audit/investigations/15", params=PERIOD, headers=GET_HEADERS
    )
    assert investigation.status_code == 200
    assert [item["match"] for item in investigation.json()["items"]] == ["actor", "target"]
    bad_id = harness.client.get(
        "/api/v1/staff/audit/investigations/abc", params=PERIOD, headers=GET_HEADERS
    )
    assert bad_id.status_code == 422

    summary = harness.client.get("/api/v1/staff/audit/summary", params=PERIOD, headers=GET_HEADERS)
    assert summary.status_code == 200
    metrics = {item["key"]: item["count"] for item in summary.json()["metrics"]}
    assert metrics["role_changes"] == 1 and summary.json()["total_events"] == 2
    catalog = harness.client.get("/api/v1/staff/audit/catalog", headers=GET_HEADERS)
    assert catalog.status_code == 200 and catalog.json()["max_report_events"] == 2_000


def test_events_summary_and_report_filter_by_the_role_recorded_on_each_event(
    harness: Harness,
) -> None:
    day = datetime(2026, 10, 5, 12, tzinfo=UTC)
    add_event(harness.factory, occurred_at=day, actor_id="15", actor_role="user")
    add_event(harness.factory, occurred_at=day, actor_id="6", actor_role="support_agent")
    add_event(harness.factory, occurred_at=day, actor_id="9", actor_role=None)
    as_role(harness, "auditor-access")

    catalog = harness.client.get("/api/v1/staff/audit/catalog", headers=GET_HEADERS)
    assert catalog.json()["actor_roles"] == ["user", "support_agent", "admin", "security_auditor"]

    everyone = harness.client.get("/api/v1/staff/audit/events", params=PERIOD, headers=GET_HEADERS)
    assert sorted(str(item["actor_role"]) for item in everyone.json()["items"]) == [
        "None",
        "support_agent",
        "user",
    ]
    customers = harness.client.get(
        "/api/v1/staff/audit/events",
        params={**PERIOD, "actor_role": "user"},
        headers=GET_HEADERS,
    )
    assert customers.status_code == 200
    assert [item["actor_id"] for item in customers.json()["items"]] == ["15"]
    assert customers.json()["filters"] == {
        "event_type": None,
        "outcome": None,
        "actor_id": None,
        "actor_role": "user",
        "target_type": None,
        "target_id": None,
        "correlation_id": None,
    }

    summary = harness.client.get(
        "/api/v1/staff/audit/summary",
        params={**PERIOD, "actor_role": "support_agent"},
        headers=GET_HEADERS,
    )
    assert summary.json()["total_events"] == 1

    unknown = harness.client.get(
        "/api/v1/staff/audit/events",
        params={**PERIOD, "actor_role": "superuser"},
        headers=GET_HEADERS,
    )
    assert unknown.status_code == 422
    assert "superuser" not in unknown.text

    report = harness.client.post(
        "/api/v1/staff/audit/reports",
        json={**PERIOD, "actor_role": "user"},
        headers=POST_HEADERS,
    )
    assert report.status_code == 200, report.text
    assert report.headers["x-report-event-count"] == "1"
    with harness.factory() as session:
        exported = session.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.event_type == "audit.report_exported"
            )
        )
    assert exported is not None and exported.context["filter_actor_role"] == "user"


def test_report_download_is_a_pdf_with_metadata_and_recorded_export(harness: Harness) -> None:
    add_event(harness.factory, occurred_at=datetime(2026, 10, 5, tzinfo=UTC))
    as_role(harness, "auditor-access")
    response = harness.client.post(
        "/api/v1/staff/audit/reports",
        json={**PERIOD, "event_type": "staff.login"},
        headers=POST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert response.headers["x-report-event-count"] == "1"
    report_id = response.headers["x-report-id"]
    assert (
        f'filename="informe-auditoria-{report_id}.pdf"' in response.headers["content-disposition"]
    )
    assert response.headers["cache-control"].startswith("no-store")
    # Delivery re-resolved the session after the request dependency did.
    assert harness.sessions.resolutions >= 2
    with harness.factory() as session:
        exported = session.scalar(
            select(SecurityAuditEvent).where(
                SecurityAuditEvent.event_type == "audit.report_exported"
            )
        )
    assert exported is not None and exported.target_resource_id == report_id


def test_report_requires_json_origin_and_valid_body(harness: Harness) -> None:
    as_role(harness, "auditor-access")
    no_origin = harness.client.post(
        "/api/v1/staff/audit/reports", json=PERIOD, headers={"X-Staff-Request": "1"}
    )
    assert no_origin.status_code == 403
    wrong_type = harness.client.post(
        "/api/v1/staff/audit/reports",
        content=b"start_date=2026-10-01",
        headers={**POST_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert wrong_type.status_code == 415
    extra = harness.client.post(
        "/api/v1/staff/audit/reports", json={**PERIOD, "sql": "x"}, headers=POST_HEADERS
    )
    assert extra.status_code == 422


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_audit_evidence_cannot_be_modified_through_the_api(harness: Harness, method: str) -> None:
    as_role(harness, "auditor-access")
    response = getattr(harness.client, method)(
        "/api/v1/staff/audit/events/00000000-0000-0000-0000-000000000001", headers=POST_HEADERS
    )
    assert response.status_code == 405


def test_security_auditor_cannot_use_support_functions(harness: Harness) -> None:
    class UnusedSupportService:
        def list_queue(self, *args: object) -> list[object]:
            raise AssertionError("support service must not be reached")

    harness.client.app.dependency_overrides[get_support_ticket_service] = UnusedSupportService  # type: ignore[attr-defined]
    as_role(harness, "auditor-access")
    response = harness.client.get("/api/v1/staff/support/tickets", headers=GET_HEADERS)
    assert response.status_code == 403
    assert harness.denials.calls[-1]["resource"] == "support"

"""AUD-01..AUD-03 read services over a real (SQLite) event store."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.policy import AuditFilters, AuditPeriod, normalize_period
from finance_ai.audit.query_service import AuditQueryService
from finance_ai.core.errors import (
    AuditEventNotFoundError,
    AuditQueryInvalidError,
    AuditQueryUnavailableError,
)
from tests.audit_support import FixedClock, add_event

NOW = datetime(2026, 11, 14, 20, 0, tzinfo=UTC)


def make_service(factory: sessionmaker[Session], now: datetime = NOW) -> AuditQueryService:
    return AuditQueryService(factory, FixedClock(now), max_query_days=90, max_report_events=2_000)


def period(start: date = date(2026, 11, 1), end: date = date(2026, 11, 14)) -> AuditPeriod:
    return normalize_period(start, end, "America/Bogota", max_days=90)


def test_list_is_newest_first_and_traverses_equal_timestamps_without_gaps(
    audit_factory: sessionmaker[Session],
) -> None:
    tied_at = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    ids = [add_event(audit_factory, occurred_at=tied_at) for _ in range(5)]
    newest = add_event(audit_factory, occurred_at=tied_at + timedelta(minutes=1))
    service = make_service(audit_factory)

    seen: list[UUID] = []
    cursor: str | None = None
    while True:
        page = service.list_events(period(), AuditFilters(), cursor=cursor, limit=2)
        seen.extend(item.event_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen[0] == newest
    assert sorted(seen[1:]) == sorted(ids)
    assert len(seen) == len(set(seen)) == 6
    assert seen[1:] == sorted(ids, reverse=True, key=lambda value: value.hex)


def test_read_boundary_keeps_later_events_out_of_an_ongoing_traversal(
    audit_factory: sessionmaker[Session],
) -> None:
    for minute in range(3):
        add_event(audit_factory, occurred_at=NOW - timedelta(hours=1, minutes=minute))
    service = make_service(audit_factory)
    first = service.list_events(period(), AuditFilters(), cursor=None, limit=2)
    assert first.read_boundary == NOW
    add_event(audit_factory, occurred_at=NOW + timedelta(seconds=1))
    later = make_service(audit_factory, NOW + timedelta(minutes=5))
    second = later.list_events(period(), AuditFilters(), cursor=first.next_cursor, limit=2)
    assert len(first.items) + len(second.items) == 3
    assert second.next_cursor is None


def test_period_boundaries_are_inclusive_start_exclusive_end(
    audit_factory: sessionmaker[Session],
) -> None:
    selected = period(date(2026, 11, 10), date(2026, 11, 10))
    inside_start = add_event(audit_factory, occurred_at=selected.start_utc)
    inside_end = add_event(audit_factory, occurred_at=selected.end_utc - timedelta(microseconds=1))
    add_event(audit_factory, occurred_at=selected.end_utc)
    add_event(audit_factory, occurred_at=selected.start_utc - timedelta(microseconds=1))

    page = make_service(audit_factory).list_events(selected, AuditFilters(), cursor=None, limit=10)
    assert {item.event_id for item in page.items} == {inside_start, inside_end}
    assert page.period.time_zone_label == "America/Bogota (UTC-05:00)"
    assert page.period.end_date == date(2026, 11, 10)


def test_combined_filters_and_sanitized_projection(audit_factory: sessionmaker[Session]) -> None:
    at = datetime(2026, 11, 12, 12, tzinfo=UTC)
    wanted = add_event(
        audit_factory,
        occurred_at=at,
        event_type="access.denied",
        result="denied",
        actor_id="9",
        target_type="protected_resource",
        target_id="security_audit",
        correlation_id="abc",
        context={"reason": "role_not_permitted", "password": "never", "token": "never"},
    )
    add_event(audit_factory, occurred_at=at, event_type="access.denied", result="denied")
    add_event(audit_factory, occurred_at=at, event_type="staff.login", actor_id="9")
    filters = AuditFilters(
        event_type="access.denied",
        outcome="denied",
        actor_id="9",
        target_type="protected_resource",
        target_id="security_audit",
        correlation_id="abc",
    )
    page = make_service(audit_factory).list_events(period(), filters, cursor=None, limit=10)
    assert [item.event_id for item in page.items] == [wanted]
    assert page.items[0].context == {"reason": "role_not_permitted"}
    assert page.filters.correlation_id == "abc"
    serialized = page.model_dump_json()
    assert "never" not in serialized and "password" not in serialized


def test_empty_results_unknown_fields_and_missing_detail(
    audit_factory: sessionmaker[Session],
) -> None:
    service = make_service(audit_factory)
    empty = service.list_events(period(), AuditFilters(), cursor=None, limit=10)
    assert empty.items == [] and empty.next_cursor is None

    event_id = add_event(
        audit_factory,
        occurred_at=datetime(2026, 11, 3, tzinfo=UTC),
        actor_id=None,
        correlation_id=None,
        event_type="staff.login_denied",
        result="denied",
    )
    detail = service.get_event(event_id)
    assert detail.actor_id is None and detail.correlation_id is None and detail.target_id is None
    with pytest.raises(AuditEventNotFoundError):
        service.get_event(UUID(int=1))


def test_cursor_from_another_scope_is_rejected(audit_factory: sessionmaker[Session]) -> None:
    for minute in range(3):
        add_event(audit_factory, occurred_at=datetime(2026, 11, 5, 12, minute, tzinfo=UTC))
    service = make_service(audit_factory)
    first = service.list_events(period(), AuditFilters(), cursor=None, limit=1)
    assert first.next_cursor is not None
    with pytest.raises(AuditQueryInvalidError):
        service.list_events(
            period(), AuditFilters(outcome="success"), cursor=first.next_cursor, limit=1
        )


def test_investigation_matches_actor_or_typed_account_target_once_oldest_first(
    audit_factory: sessionmaker[Session],
) -> None:
    base = datetime(2026, 11, 8, 12, tzinfo=UTC)
    actor_only = add_event(audit_factory, occurred_at=base, actor_id="15")
    target_only = add_event(
        audit_factory,
        occurred_at=base + timedelta(minutes=1),
        actor_id="1",
        target_type="account",
        target_id="15",
        event_type="auth.mfa_failed",
        result="failure",
    )
    both = add_event(
        audit_factory,
        occurred_at=base + timedelta(minutes=2),
        actor_id="15",
        target_type="account",
        target_id="15",
        event_type="auth.mfa_success",
    )
    # Same identifier in another namespace must not collide with the account.
    add_event(
        audit_factory,
        occurred_at=base,
        actor_id="2",
        target_type="support_ticket",
        target_id="15",
        event_type="support.ticket_claimed",
    )
    add_event(audit_factory, occurred_at=base, actor_id="150")

    page = make_service(audit_factory).investigate_account("15", period(), cursor=None, limit=2)
    assert page.identity_lookup == "not_performed"
    assert [(item.event_id, item.match) for item in page.items] == [
        (actor_only, "actor"),
        (target_only, "target"),
    ]
    rest = make_service(audit_factory).investigate_account(
        "15", period(), cursor=page.next_cursor, limit=2
    )
    assert [(item.event_id, item.match) for item in rest.items] == [(both, "actor_and_target")]
    assert rest.next_cursor is None


def test_investigation_of_an_identifier_without_a_current_account_is_valid(
    audit_factory: sessionmaker[Session],
) -> None:
    add_event(audit_factory, occurred_at=datetime(2026, 11, 2, tzinfo=UTC), actor_id="999")
    page = make_service(audit_factory).investigate_account("999", period(), cursor=None, limit=5)
    assert len(page.items) == 1 and page.items[0].actor_id == "999"


def test_summary_counts_the_complete_filtered_dataset_with_zone_buckets(
    audit_factory: sessionmaker[Session],
) -> None:
    for minute in range(30):  # more rows than any page size
        add_event(
            audit_factory,
            occurred_at=datetime(2026, 11, 10, 12, minute, tzinfo=UTC),
            event_type="auth.login_success",
        )
    add_event(
        audit_factory,
        occurred_at=datetime(2026, 11, 10, 13, tzinfo=UTC),
        event_type="auth.mfa_success",
    )
    add_event(
        audit_factory,
        occurred_at=datetime(2026, 11, 11, 3, tzinfo=UTC),  # 2026-11-10 22:00 in Bogota
        event_type="auth.mfa_failed",
        result="failure",
    )
    add_event(
        audit_factory,
        occurred_at=datetime(2026, 11, 12, 6, tzinfo=UTC),
        event_type="staff.login_denied",
        result="denied",
        actor_id=None,
    )
    add_event(
        audit_factory,
        occurred_at=datetime(2026, 11, 12, 7, tzinfo=UTC),
        event_type="access.denied",
        result="denied",
    )
    summary = make_service(audit_factory).summary(period(), AuditFilters())

    metrics = {metric.key: metric.count for metric in summary.metrics}
    assert summary.total_events == 34
    assert metrics == {
        "successful_logins": 30,
        "failed_logins": 1,
        "failed_mfa": 1,
        "access_denied": 1,
        "account_suspensions": 0,
        "role_changes": 0,
    }
    by_day = {item.date: item.count for item in summary.by_day}
    assert len(by_day) == 14
    assert by_day[date(2026, 11, 10)] == 32
    assert by_day[date(2026, 11, 11)] == 0
    assert by_day[date(2026, 11, 12)] == 2
    assert sum(by_day.values()) == summary.total_events
    assert summary.by_type[0].event_type == "auth.login_success"
    assert summary.cutoff == NOW
    assert summary.coverage == "partial"
    mapping = next(metric for metric in summary.metrics if metric.key == "successful_logins")
    assert [item.event_type for item in mapping.mapping] == ["auth.login_success", "staff.login"]

    filtered = make_service(audit_factory).summary(period(), AuditFilters(outcome="denied"))
    assert filtered.total_events == 2
    assert {item.event_type for item in filtered.by_type} == {"staff.login_denied", "access.denied"}


def test_summary_distinguishes_zero_activity_from_missing_coverage(
    audit_factory: sessionmaker[Session],
) -> None:
    service = make_service(audit_factory)
    empty_store = service.summary(period(), AuditFilters())
    assert empty_store.total_events == 0 and empty_store.coverage == "no_records"
    assert all(item.count == 0 for item in empty_store.by_day)

    add_event(audit_factory, occurred_at=datetime(2026, 10, 1, tzinfo=UTC))
    zero = service.summary(period(), AuditFilters())
    assert zero.total_events == 0 and zero.coverage == "available"
    assert zero.earliest_recorded_at == datetime(2026, 10, 1, tzinfo=UTC)


def test_storage_failures_are_errors_not_empty_results() -> None:
    broken = sessionmaker(bind=create_engine("sqlite://"))  # no event table
    service = make_service(broken)
    with pytest.raises(AuditQueryUnavailableError):
        service.list_events(period(), AuditFilters(), cursor=None, limit=5)
    with pytest.raises(AuditQueryUnavailableError):
        service.summary(period(), AuditFilters())
    with pytest.raises(AuditQueryUnavailableError):
        service.investigate_account("1", period(), cursor=None, limit=5)


def test_catalog_publishes_bounds_and_documented_mappings(
    audit_factory: sessionmaker[Session],
) -> None:
    catalog = make_service(audit_factory).catalog()
    assert catalog.max_query_days == 90 and catalog.max_report_events == 2_000
    assert catalog.default_time_zone == "America/Bogota"
    assert "audit.report_exported" in catalog.event_types
    assert {metric.key for metric in catalog.metrics} >= {"successful_logins", "failed_mfa"}

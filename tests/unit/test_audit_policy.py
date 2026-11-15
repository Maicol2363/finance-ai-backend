from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from finance_ai.audit.policy import (
    SUMMARY_METRICS,
    AuditCursor,
    AuditFilters,
    decode_cursor,
    encode_cursor,
    local_day,
    normalize_period,
    project_context,
    scope_fingerprint,
)
from finance_ai.core.errors import AuditPeriodTooLongError, AuditQueryInvalidError


def test_bogota_dates_map_to_inclusive_start_and_exclusive_next_midnight() -> None:
    period = normalize_period(date(2026, 11, 1), date(2026, 11, 14), "America/Bogota", max_days=90)
    assert period.start_utc == datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
    assert period.end_utc == datetime(2026, 11, 15, 5, 0, tzinfo=UTC)
    assert len(period.days()) == 14
    utc_period = normalize_period(date(2026, 11, 1), date(2026, 11, 1), "UTC", max_days=90)
    assert utc_period.end_utc - utc_period.start_utc == timedelta(days=1)


def test_period_limits_are_validated_after_normalization() -> None:
    start = date(2026, 1, 1)
    assert normalize_period(start, start + timedelta(days=89), "America/Bogota", max_days=90)
    with pytest.raises(AuditPeriodTooLongError) as too_long:
        normalize_period(start, start + timedelta(days=90), "America/Bogota", max_days=90)
    assert too_long.value.code == "audit_period_too_long"
    with pytest.raises(AuditQueryInvalidError) as reversed_period:
        normalize_period(start, start - timedelta(days=1), "UTC", max_days=90)
    assert "end_date" in (reversed_period.value.field_errors or {})
    with pytest.raises(AuditQueryInvalidError):
        normalize_period(start, start, "Europe/Madrid", max_days=90)


def test_local_day_uses_the_reporting_zone() -> None:
    period = normalize_period(date(2026, 11, 14), date(2026, 11, 14), "America/Bogota", max_days=1)
    assert local_day(datetime(2026, 11, 15, 4, 59, tzinfo=UTC), period.zone) == date(2026, 11, 14)
    assert local_day(datetime(2026, 11, 15, 5, 0), period.zone) == date(2026, 11, 15)


def test_target_identifier_requires_a_typed_namespace_and_export_context_is_sanitized() -> None:
    with pytest.raises(AuditQueryInvalidError):
        AuditFilters(target_id="15")
    filters = AuditFilters(event_type="access.denied", target_type="account", target_id="15")
    assert filters.as_export_context() == {
        "filter_event_type": "access.denied",
        "filter_target_type": "account",
        "filter_target_id": "15",
    }


def test_cursor_round_trip_is_bound_to_its_query_scope() -> None:
    period = normalize_period(date(2026, 11, 1), date(2026, 11, 2), "UTC", max_days=90)
    scope = scope_fingerprint("events", period, AuditFilters(), "")
    other_scope = scope_fingerprint("events", period, AuditFilters(outcome="denied"), "")
    cursor = AuditCursor(
        occurred_at=datetime(2026, 11, 1, 12, tzinfo=UTC),
        event_id=uuid4(),
        read_boundary=datetime(2026, 11, 2, tzinfo=UTC),
    )
    raw = encode_cursor(cursor, scope)
    assert decode_cursor(raw, scope) == cursor
    for bad in (raw, "not-base64!", "e30", raw[:-4]):
        with pytest.raises(AuditQueryInvalidError):
            decode_cursor(bad, other_scope if bad == raw else scope)


def test_projection_is_an_explicit_allowlist() -> None:
    projected = project_context(
        {
            "reason": "invalid_code",
            "password": "secret",
            "token": "abc",
            "amount": "100",
            "report_id": 7,
            "resource": "x" * 129,
            "event_count": "3",
        }
    )
    assert projected == {"reason": "invalid_code", "event_count": "3"}
    assert project_context(["reason"]) == {}


def test_metric_mappings_keep_login_and_factor_events_separate() -> None:
    counts = {
        ("auth.login_success", "success"): 2,
        ("staff.login", "success"): 1,
        ("auth.mfa_success", "success"): 5,
        ("auth.mfa_failed", "failure"): 3,
        ("staff.login_denied", "denied"): 4,
        ("access.denied", "denied"): 6,
        ("account.suspended", "success"): 1,
        ("account.suspended", "failure"): 9,
        ("role.changed", "success"): 2,
    }
    values = {metric.key: metric.count(counts) for metric in SUMMARY_METRICS}
    assert values == {
        "successful_logins": 3,
        "failed_logins": 4,
        "failed_mfa": 3,
        "access_denied": 6,
        "account_suspensions": 1,
        "role_changes": 2,
    }

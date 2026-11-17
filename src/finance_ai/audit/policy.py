"""Deterministic Security Auditor query policy shared by AUD-01..AUD-04.

Everything here is pure: period normalization, filter scope, stable cursors, the reader
field allowlist and the documented summary metric mappings.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Final, Literal
from uuid import UUID

from finance_ai.core.errors import AuditPeriodTooLongError, AuditQueryInvalidError

TimeZoneName = Literal["America/Bogota", "UTC"]
AuditOutcome = Literal["success", "denied", "failure"]
AuditTargetType = Literal[
    "account",
    "audit_report",
    "protected_resource",
    "staff_invitation",
    "staff_session",
    "support_ticket",
]
# The four fixed authorization roles, as recorded on each event at the time it happened.
AuditActorRole = Literal["user", "support_agent", "admin", "security_auditor"]
AUDIT_ACTOR_ROLES: Final[tuple[str, ...]] = ("user", "support_agent", "admin", "security_auditor")

DEFAULT_TIME_ZONE: Final[TimeZoneName] = "America/Bogota"
# Colombia has observed UTC-05:00 without daylight saving time since 1993, so the
# reporting zone is an exact fixed offset and needs no host time-zone database.
REPORTING_TIME_ZONES: Final[dict[str, tzinfo]] = {
    "America/Bogota": timezone(timedelta(hours=-5), "America/Bogota"),
    "UTC": UTC,
}
TIME_ZONE_LABELS: Final[dict[str, str]] = {
    "America/Bogota": "America/Bogota (UTC-05:00)",
    "UTC": "UTC (UTC+00:00)",
}
AUDIT_OUTCOMES: Final[tuple[str, ...]] = ("success", "denied", "failure")
AUDIT_TARGET_TYPES: Final[tuple[str, ...]] = (
    "account",
    "audit_report",
    "protected_resource",
    "staff_invitation",
    "staff_session",
    "support_ticket",
)
# Typed affected-account namespace used for historical account investigation.
ACCOUNT_TARGET_TYPE: Final = "account"

EVENT_TYPE_PATTERN: Final = r"^[a-z][a-z0-9_]{0,39}(\.[a-z0-9_]{1,40}){1,3}$"
ACTOR_ID_PATTERN: Final = r"^[1-9][0-9]{0,18}$"
TARGET_ID_PATTERN: Final = r"^[A-Za-z0-9._:-]{1,128}$"
CORRELATION_ID_PATTERN: Final = r"^[A-Za-z0-9._-]{1,64}$"
CURSOR_PATTERN: Final = r"^[A-Za-z0-9_-]{1,512}$"
DEFAULT_PAGE_SIZE: Final = 25
MAX_PAGE_SIZE: Final = 100

# SEC-02 approved catalog plus identifiers already emitted by implemented modules.
KNOWN_EVENT_TYPES: Final[tuple[str, ...]] = tuple(
    sorted(
        {
            "access.denied",
            "account.reactivated",
            "account.suspended",
            "audit.report_exported",
            "auth.login_failed",
            "auth.login_success",
            "auth.logout",
            "auth.mfa_enrolled",
            "auth.mfa_failed",
            "auth.mfa_recovered",
            "auth.mfa_replaced",
            "auth.mfa_success",
            "role.changed",
            "session.revoked",
            "staff.activated",
            "staff.created",
            "staff.login",
            "staff.login_denied",
            "staff.logout",
            "staff.refresh",
            "staff.session_revoked",
            "support.customer_lookup",
            "support.message_sent",
            "support.ticket_claimed",
            "support.ticket_created",
            "support.ticket_resolved",
            "support.ticket_returned",
            "ticket.escalated",
        }
    )
)

# Reader allowlist: nothing outside these keys, and only bounded strings, is projected to
# lists, details, investigations, aggregates or PDFs.
PROJECTED_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "reason",
        "resource",
        "report_id",
        "period_start",
        "period_end",
        "time_zone",
        "event_count",
        "filter_event_type",
        "filter_outcome",
        "filter_actor_id",
        "filter_target_type",
        "filter_target_id",
        "filter_correlation_id",
        "filter_actor_role",
        "previous_role",
        "new_role",
        "channel",
        "platform",
    }
)
_MAX_PROJECTED_VALUE_LENGTH: Final = 128


@dataclass(frozen=True)
class AuditPeriod:
    """Calendar dates in the reporting zone mapped to a UTC [start, end) interval."""

    start_date: date
    end_date: date
    time_zone: str
    start_utc: datetime
    end_utc: datetime

    @property
    def zone(self) -> tzinfo:
        return REPORTING_TIME_ZONES[self.time_zone]

    def days(self) -> list[date]:
        return [
            self.start_date + timedelta(days=offset)
            for offset in range((self.end_date - self.start_date).days + 1)
        ]


def normalize_period(
    start_date: date, end_date: date, time_zone: str, *, max_days: int
) -> AuditPeriod:
    if time_zone not in REPORTING_TIME_ZONES:
        raise AuditQueryInvalidError({"time_zone": ["Select a supported reporting time zone."]})
    if end_date < start_date:
        raise AuditQueryInvalidError(
            {"end_date": ["The end date must be on or after the start date."]}
        )
    zone = REPORTING_TIME_ZONES[time_zone]
    start_utc = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(UTC)
    # A selected end date covers that whole day: the exclusive end is the next midnight.
    end_utc = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    if end_utc - start_utc > timedelta(days=max_days):
        raise AuditPeriodTooLongError(max_days)
    return AuditPeriod(
        start_date=start_date,
        end_date=end_date,
        time_zone=time_zone,
        start_utc=start_utc,
        end_utc=end_utc,
    )


@dataclass(frozen=True)
class AuditFilters:
    event_type: str | None = None
    outcome: str | None = None
    actor_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    correlation_id: str | None = None
    actor_role: str | None = None

    def __post_init__(self) -> None:
        if self.target_id is not None and self.target_type is None:
            raise AuditQueryInvalidError(
                {"target_type": ["Select the target type for this target identifier."]}
            )

    def as_dict(self) -> dict[str, str]:
        values = {
            "event_type": self.event_type,
            "outcome": self.outcome,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "correlation_id": self.correlation_id,
        }
        return {key: value for key, value in values.items() if value is not None}

    def as_export_context(self) -> dict[str, str]:
        return {f"filter_{key}": value for key, value in self.as_dict().items()}


@dataclass(frozen=True)
class AuditCursor:
    occurred_at: datetime
    event_id: UUID
    read_boundary: datetime


def scope_fingerprint(kind: str, period: AuditPeriod, filters: AuditFilters, subject: str) -> str:
    payload = json.dumps(
        {
            "kind": kind,
            "start": period.start_utc.isoformat(),
            "end": period.end_utc.isoformat(),
            "filters": filters.as_dict(),
            "subject": subject,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def encode_cursor(cursor: AuditCursor, scope: str) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "t": cursor.occurred_at.astimezone(UTC).isoformat(),
            "e": cursor.event_id.hex,
            "b": cursor.read_boundary.astimezone(UTC).isoformat(),
            "s": scope,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(raw: str, scope: str) -> AuditCursor:
    invalid = AuditQueryInvalidError(
        {"cursor": ["The page position is no longer valid. Reload the first page."]}
    )
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("s") != scope:
            raise invalid
        occurred_at = _aware(datetime.fromisoformat(str(payload["t"])))
        read_boundary = _aware(datetime.fromisoformat(str(payload["b"])))
        event_id = UUID(hex=str(payload["e"]))
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise invalid from exc
    return AuditCursor(occurred_at=occurred_at, event_id=event_id, read_boundary=read_boundary)


def project_context(context: object) -> dict[str, str]:
    if not isinstance(context, Mapping):
        return {}
    return {
        key: value
        for key, value in context.items()
        if key in PROJECTED_CONTEXT_KEYS
        and isinstance(value, str)
        and len(value) <= _MAX_PROJECTED_VALUE_LENGTH
    }


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    label: str
    description: str
    mapping: tuple[tuple[str, tuple[str, ...]], ...]

    def count(self, by_type_outcome: Mapping[tuple[str, str], int]) -> int:
        return sum(
            by_type_outcome.get((event_type, outcome), 0)
            for event_type, outcomes in self.mapping
            for outcome in outcomes
        )


# Documented event-type/outcome mappings (AUD-03). Failed logins and failed factors are
# different event measures: they are not additive counts of people or attacks.
SUMMARY_METRICS: Final[tuple[MetricDefinition, ...]] = (
    MetricDefinition(
        key="successful_logins",
        label="Inicios de sesión exitosos",
        description=(
            "Inicios de sesión completados. Para security_auditor y admin solo cuentan tras "
            "contraseña y MFA; auth.mfa_success es un evento del factor y no se cuenta aquí. "
            "staff.login es el inicio de sesión completado de un agente de soporte."
        ),
        mapping=(("auth.login_success", ("success",)), ("staff.login", ("success",))),
    ),
    MetricDefinition(
        key="failed_logins",
        label="Inicios de sesión fallidos",
        description=(
            "Intentos de inicio de sesión rechazados (credenciales inválidas, cuenta no "
            "elegible o sesión existente). No incluye fallos del segundo factor."
        ),
        mapping=(
            ("auth.login_failed", ("failure", "denied")),
            ("staff.login_denied", ("denied",)),
        ),
    ),
    MetricDefinition(
        key="failed_mfa",
        label="Verificaciones MFA fallidas",
        description=(
            "Códigos TOTP rechazados (inválidos o reutilizados). Es una medida del factor, "
            "distinta de los inicios de sesión fallidos."
        ),
        mapping=(("auth.mfa_failed", ("failure", "denied")),),
    ),
    MetricDefinition(
        key="access_denied",
        label="Accesos denegados",
        description="Solicitudes autenticadas rechazadas a un recurso protegido.",
        mapping=(("access.denied", ("denied",)),),
    ),
    MetricDefinition(
        key="account_suspensions",
        label="Suspensiones de cuenta",
        description="Suspensiones de cuenta completadas con éxito.",
        mapping=(("account.suspended", ("success",)),),
    ),
    MetricDefinition(
        key="role_changes",
        label="Cambios de rol del personal",
        description="Cambios de rol del personal completados con éxito.",
        mapping=(("role.changed", ("success",)),),
    ),
)


def local_day(moment: datetime, zone: tzinfo) -> date:
    return _aware(moment).astimezone(zone).date()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize persisted timestamps (naive on SQLite) to aware UTC."""
    return _aware(value)

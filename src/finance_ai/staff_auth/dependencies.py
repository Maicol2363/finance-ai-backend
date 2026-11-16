from __future__ import annotations

import ipaddress
from datetime import timedelta
from functools import lru_cache
from os import getenv
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, Request
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from finance_ai.audit.service import AccessDenialRecorder, SecurityAuditWriter
from finance_ai.auth.dependencies import (
    get_credential_codec,
    get_password_hasher,
    get_unsafe_password_provider,
)
from finance_ai.auth.session_policy import SystemClock
from finance_ai.core.config import Settings, get_settings
from finance_ai.core.errors import (
    MfaRateLimitedError,
    StaffAccessDeniedError,
    StaffOriginDeniedError,
    StaffRequestDeniedError,
)
from finance_ai.core.roles import ADMIN_ROLE, SECURITY_AUDITOR_ROLE, SUPPORT_AGENT_ROLE
from finance_ai.db.session import get_session_factory
from finance_ai.staff_auth.activation import StaffActivationService
from finance_ai.staff_auth.mfa import StaffMfaPolicy
from finance_ai.staff_auth.recovery import StaffRecoveryService
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.staff_auth.service import StaffLifetimes, StaffSessionService

StaffCookieSettings = tuple[bool, Literal["strict"], bool]


def get_staff_session_service() -> StaffSessionService:
    settings = get_settings()
    return StaffSessionService(
        session_factory=get_session_factory(),
        password_hasher=get_password_hasher(),
        credential_codec=get_credential_codec(),
        clock=SystemClock(),
        audit_writer=SecurityAuditWriter(),
        mfa=_staff_mfa_policy(),
        admin_lifetimes=StaffLifetimes(
            access=timedelta(minutes=settings.staff_admin_access_minutes),
            idle=timedelta(minutes=settings.staff_admin_idle_minutes),
            absolute=timedelta(hours=settings.staff_admin_absolute_hours),
        ),
    )


def get_staff_activation_service() -> StaffActivationService:
    return StaffActivationService(
        session_factory=get_session_factory(),
        password_hasher=get_password_hasher(),
        unsafe_passwords=get_unsafe_password_provider(),
        credential_codec=get_credential_codec(),
        clock=SystemClock(),
        audit_writer=SecurityAuditWriter(),
    )


def get_staff_recovery_service() -> StaffRecoveryService:
    return StaffRecoveryService(
        session_factory=get_session_factory(),
        password_hasher=get_password_hasher(),
        credential_codec=get_credential_codec(),
        clock=SystemClock(),
        audit_writer=SecurityAuditWriter(),
        mfa=_staff_mfa_policy(),
    )


def _staff_mfa_policy() -> StaffMfaPolicy | None:
    return StaffMfaPolicy.from_settings(get_settings())


def get_access_denial_recorder() -> AccessDenialRecorder:
    return AccessDenialRecorder(get_session_factory(), SecurityAuditWriter())


@lru_cache
def _mfa_limiter() -> FixedWindowRateLimiter:
    return FixedWindowRateLimiter(MemoryStorage())


def enforce_staff_mfa_limit(request: Request) -> None:
    """Bound second-factor attempts per client in addition to the per-challenge limit."""
    peer = request.client.host if request.client else "unknown"
    if not _mfa_limiter().hit(parse("10/minute"), "staff-mfa", peer):
        raise MfaRateLimitedError(60)


def enforce_staff_activation_limit(request: Request) -> None:
    """Own bucket: a client hammering activation must not lock others out of MFA."""
    peer = request.client.host if request.client else "unknown"
    if not _mfa_limiter().hit(parse("10/minute"), "staff-activate", peer):
        raise MfaRateLimitedError(60)


def enforce_staff_recovery_limit(request: Request) -> None:
    """Own bucket for authenticator recovery attempts (password + proof)."""
    peer = request.client.host if request.client else "unknown"
    if not _mfa_limiter().hit(parse("10/minute"), "staff-recover", peer):
        raise MfaRateLimitedError(60)


def require_staff_request_boundary(request: Request) -> None:
    if request.headers.get("x-staff-request") != "1":
        raise StaffRequestDeniedError()
    if request.headers.get("authorization") is not None:
        raise StaffRequestDeniedError()
    if request.headers.get("sec-fetch-site", "").casefold() == "cross-site":
        raise StaffOriginDeniedError()

    origin = request.headers.get("origin")
    settings = get_settings()
    if request.method == "GET" and origin is None:
        if request.headers.get("sec-fetch-site", "").casefold() != "same-origin":
            raise StaffOriginDeniedError()
        return
    if origin is None or origin == "null" or origin not in settings.cors_allowed_origins:
        raise StaffOriginDeniedError()
    parsed = urlsplit(origin)
    if parsed.scheme == "https":
        return
    if not _is_allowed_local_origin(parsed, settings):
        raise StaffOriginDeniedError()


def get_staff_access_user(
    request: Request,
    _boundary: Annotated[None, Depends(require_staff_request_boundary)],
    service: Annotated[StaffSessionService, Depends(get_staff_session_service)],
) -> StaffSessionUser:
    access_cookie, _ = staff_cookie_names()
    raw_access = request.cookies.get(access_cookie)
    return service.resolve(
        raw_access,
        correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
    )


def _require_role(
    request: Request,
    user: StaffSessionUser,
    recorder: AccessDenialRecorder,
    *,
    role: str,
    resource: str,
) -> StaffSessionUser:
    if user.role != role:
        recorder.record(
            actor_id=user.id,
            resource=resource,
            reason="role_not_permitted",
            correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
        )
        raise StaffAccessDeniedError()
    return user


def get_support_agent_user(
    request: Request,
    user: Annotated[StaffSessionUser, Depends(get_staff_access_user)],
    recorder: Annotated[AccessDenialRecorder, Depends(get_access_denial_recorder)],
) -> StaffSessionUser:
    return _require_role(request, user, recorder, role=SUPPORT_AGENT_ROLE, resource="support")


def get_security_auditor_user(
    request: Request,
    user: Annotated[StaffSessionUser, Depends(get_staff_access_user)],
    recorder: Annotated[AccessDenialRecorder, Depends(get_access_denial_recorder)],
) -> StaffSessionUser:
    """Active security_auditor whose session completed password+TOTP.

    ``get_staff_access_user`` already rejects (and revokes) Auditor sessions without MFA
    assurance, suspended accounts and non-portal roles on every request.
    """
    return _require_role(
        request, user, recorder, role=SECURITY_AUDITOR_ROLE, resource="security_audit"
    )


def get_admin_user(
    request: Request,
    user: Annotated[StaffSessionUser, Depends(get_staff_access_user)],
    recorder: Annotated[AccessDenialRecorder, Depends(get_access_denial_recorder)],
) -> StaffSessionUser:
    """Active admin whose session completed password+TOTP with an active authenticator.

    ``get_staff_access_user`` re-evaluates the current role, active status, MFA assurance
    and authenticator on every request, so a stale or pre-promotion session never works.
    """
    return _require_role(request, user, recorder, role=ADMIN_ROLE, resource="administration")


def staff_cookie_names() -> tuple[str, str]:
    settings = get_settings()
    if settings.environment == "production":
        return "__Host-finance_staff_access", "__Host-finance_staff_refresh"
    return "finance_staff_access", "finance_staff_refresh"


def staff_mfa_cookie_name() -> str:
    if get_settings().environment == "production":
        return "__Host-finance_staff_mfa"
    return "finance_staff_mfa"


def staff_cookie_settings(request: Request) -> tuple[bool, Literal["strict"], bool]:
    settings = get_settings()
    parsed = urlsplit(str(request.base_url))
    # The configured default run.app endpoint only accepts HTTPS at Google's edge.
    # Cloud Run terminates TLS before forwarding to ASGI. Keep this exception scoped
    # to that managed endpoint without using client-provided forwarding headers.
    managed_tls = (
        settings.environment == "production"
        and parsed.scheme == "http"
        and bool(getenv("K_SERVICE"))
        and settings.staff_cloud_run_host is not None
        and parsed.netloc == settings.staff_cloud_run_host
    )
    if parsed.scheme == "https" or managed_tls:
        return True, "strict", True
    if (
        settings.environment == "production"
        or not settings.staff_local_cookie_exception_enabled
        or not _is_loopback_host(parsed.hostname)
    ):
        raise StaffOriginDeniedError()
    return False, "strict", True


def require_staff_cookie_transport(request: Request) -> StaffCookieSettings:
    """Validate cookie transport before a login, refresh, or logout can mutate state."""
    return staff_cookie_settings(request)


def _is_allowed_local_origin(parsed: object, settings: Settings) -> bool:
    if settings.environment == "production" or not settings.staff_local_cookie_exception_enabled:
        return False
    scheme = getattr(parsed, "scheme", "")
    hostname = getattr(parsed, "hostname", None)
    return scheme == "http" and _is_loopback_host(hostname)


def _is_loopback_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False

from __future__ import annotations

import ipaddress
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, Request

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.dependencies import get_credential_codec, get_password_hasher
from finance_ai.auth.session_policy import SystemClock
from finance_ai.core.config import Settings, get_settings
from finance_ai.core.errors import StaffOriginDeniedError, StaffRequestDeniedError
from finance_ai.db.session import get_session_factory
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.staff_auth.service import StaffSessionService


def get_staff_session_service() -> StaffSessionService:
    return StaffSessionService(
        session_factory=get_session_factory(),
        password_hasher=get_password_hasher(),
        credential_codec=get_credential_codec(),
        clock=SystemClock(),
        audit_writer=SecurityAuditWriter(),
    )


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


def staff_cookie_names() -> tuple[str, str]:
    settings = get_settings()
    if settings.environment == "production":
        return "__Host-finance_staff_access", "__Host-finance_staff_refresh"
    return "finance_staff_access", "finance_staff_refresh"


def staff_cookie_settings(request: Request) -> tuple[bool, Literal["strict"], bool]:
    settings = get_settings()
    parsed = urlsplit(str(request.base_url))
    if settings.environment == "production" and parsed.scheme != "https":
        raise StaffOriginDeniedError()
    if parsed.scheme == "https":
        return True, "strict", True
    if not settings.staff_local_cookie_exception_enabled or not _is_loopback_host(parsed.hostname):
        raise StaffOriginDeniedError()
    return False, "strict", True


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

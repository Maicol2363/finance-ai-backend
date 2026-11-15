"""FastAPI wiring for the Administrator services."""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Path, Request
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from finance_ai.admin.accounts_service import AdminAccountService
from finance_ai.admin.schemas import MAX_BIGINT
from finance_ai.admin.staff_service import AdminStaffService
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.dependencies import get_credential_codec
from finance_ai.auth.session_policy import SystemClock
from finance_ai.core.config import get_settings
from finance_ai.core.errors import AdminRateLimitedError
from finance_ai.db.session import get_session_factory
from finance_ai.staff_auth.dependencies import get_admin_user
from finance_ai.staff_auth.schemas import StaffSessionUser

AdminUser = Annotated[StaffSessionUser, Depends(get_admin_user)]
AccountIdPath = Annotated[int, Path(ge=1, le=MAX_BIGINT)]


@lru_cache
def _limiter() -> FixedWindowRateLimiter:
    return FixedWindowRateLimiter(MemoryStorage())


def enforce_admin_read_limit(admin: AdminUser) -> StaffSessionUser:
    """ADM-01 #3: bound lookups per administrator (in-process, like the other limiters)."""
    if not _limiter().hit(parse("60/minute"), "admin-read", str(admin.id)):
        raise AdminRateLimitedError()
    return admin


def enforce_admin_write_limit(admin: AdminUser) -> StaffSessionUser:
    if not _limiter().hit(parse("30/minute"), "admin-write", str(admin.id)):
        raise AdminRateLimitedError()
    return admin


ReadingAdmin = Annotated[StaffSessionUser, Depends(enforce_admin_read_limit)]
WritingAdmin = Annotated[StaffSessionUser, Depends(enforce_admin_write_limit)]


def get_admin_account_service() -> AdminAccountService:
    return AdminAccountService(get_session_factory(), SecurityAuditWriter())


AccountService = Annotated[AdminAccountService, Depends(get_admin_account_service)]


def get_admin_staff_service() -> AdminStaffService:
    return AdminStaffService(
        get_session_factory(),
        SecurityAuditWriter(),
        get_credential_codec(),
        SystemClock(),
        invitation_lifetime=timedelta(hours=get_settings().staff_invitation_hours),
    )


StaffService = Annotated[AdminStaffService, Depends(get_admin_staff_service)]


def correlation_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "unavailable"))

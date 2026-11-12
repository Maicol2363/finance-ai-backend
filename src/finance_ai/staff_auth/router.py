from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict

from finance_ai.auth.dependencies import get_session_rate_limiter
from finance_ai.core.errors import ProblemResponse
from finance_ai.core.rate_limit import AuthRateLimiter
from finance_ai.staff_auth.dependencies import (
    get_staff_access_user,
    get_staff_session_service,
    require_staff_request_boundary,
    staff_cookie_names,
    staff_cookie_settings,
)
from finance_ai.staff_auth.schemas import (
    StaffLoginCommand,
    StaffLoginRequest,
    StaffSessionUser,
    StaffSessionView,
)
from finance_ai.staff_auth.service import StaffSessionService

router = APIRouter(prefix="/staff/auth", tags=["Staff browser authentication"])


class EmptyStaffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _session_view(
    user: StaffSessionUser,
    access_expires_at: datetime,
    idle_expires_at: datetime,
    absolute_expires_at: datetime,
) -> StaffSessionView:
    return StaffSessionView(
        user=user,
        access_expires_at=access_expires_at,
        renewable_expires_at=idle_expires_at,
        absolute_expires_at=absolute_expires_at,
    )


def _set_session_cookies(
    response: Response,
    request: Request,
    *,
    access: str,
    refresh: str,
    access_expires_at: datetime,
    idle_expires_at: datetime,
) -> None:
    access_name, refresh_name = staff_cookie_names()
    secure, same_site, http_only = staff_cookie_settings(request)
    now = datetime.now(UTC)
    access_max_age = max(0, int((access_expires_at - now).total_seconds()))
    refresh_max_age = max(0, int((idle_expires_at - now).total_seconds()))
    response.set_cookie(
        access_name,
        access,
        max_age=access_max_age,
        path="/",
        secure=secure,
        httponly=http_only,
        samesite=same_site,
    )
    response.set_cookie(
        refresh_name,
        refresh,
        max_age=refresh_max_age,
        path="/",
        secure=secure,
        httponly=http_only,
        samesite=same_site,
    )


def _clear_session_cookies(response: Response, request: Request) -> None:
    secure, same_site, http_only = staff_cookie_settings(request)
    for cookie_name in staff_cookie_names():
        response.delete_cookie(
            cookie_name,
            path="/",
            secure=secure,
            httponly=http_only,
            samesite=same_site,
        )


@router.post(
    "/login",
    response_model=StaffSessionView,
    responses={
        401: {"model": ProblemResponse},
        409: {"model": ProblemResponse},
        413: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        429: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def staff_login(
    payload: StaffLoginRequest,
    request: Request,
    response: Response,
    _boundary: Annotated[None, Depends(require_staff_request_boundary)],
    service: Annotated[StaffSessionService, Depends(get_staff_session_service)],
    limiter: Annotated[AuthRateLimiter, Depends(get_session_rate_limiter)],
) -> StaffSessionView:
    client_key = request.client.host if request.client is not None else "unknown"
    limiter.check_login(client_key=client_key, normalized_email=payload.email.strip().lower())
    user, access, refresh, access_expires_at, idle_expires_at, absolute_expires_at = service.login(
        StaffLoginCommand(email=payload.email, password=payload.password.get_secret_value()),
        correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
    )
    _set_session_cookies(
        response,
        request,
        access=access,
        refresh=refresh,
        access_expires_at=access_expires_at,
        idle_expires_at=idle_expires_at,
    )
    return _session_view(user, access_expires_at, idle_expires_at, absolute_expires_at)


@router.get(
    "/session",
    response_model=StaffSessionView,
    responses={
        401: {"model": ProblemResponse},
        403: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def staff_session(
    request: Request,
    _: Annotated[None, Depends(require_staff_request_boundary)],
    user: Annotated[StaffSessionUser, Depends(get_staff_access_user)],
    service: Annotated[StaffSessionService, Depends(get_staff_session_service)],
) -> StaffSessionView:
    access_name, _refresh_name = staff_cookie_names()
    raw_access = request.cookies.get(access_name)
    details = service.session_projection(
        raw_access,
        correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
    )
    return _session_view(*details)


@router.post(
    "/refresh",
    response_model=StaffSessionView,
    responses={
        401: {"model": ProblemResponse},
        403: {"model": ProblemResponse},
        413: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        429: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def staff_refresh(
    payload: EmptyStaffRequest,
    request: Request,
    response: Response,
    _: Annotated[None, Depends(require_staff_request_boundary)],
    service: Annotated[StaffSessionService, Depends(get_staff_session_service)],
    limiter: Annotated[AuthRateLimiter, Depends(get_session_rate_limiter)],
) -> StaffSessionView:
    del payload
    client_key = request.client.host if request.client is not None else "unknown"
    limiter.check_refresh(client_key=client_key)
    _access_name, refresh_name = staff_cookie_names()
    (
        user,
        access,
        refresh,
        access_expires_at,
        idle_expires_at,
        absolute_expires_at,
    ) = service.refresh(
        request.cookies.get(refresh_name),
        correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
    )
    _set_session_cookies(
        response,
        request,
        access=access,
        refresh=refresh,
        access_expires_at=access_expires_at,
        idle_expires_at=idle_expires_at,
    )
    return _session_view(user, access_expires_at, idle_expires_at, absolute_expires_at)


@router.post(
    "/logout",
    summary="Staff Logout",
    description=(
        "Revokes the current staff browser session and clears its cookies. If the refresh cookie "
        "is absent or does not identify a staff browser session, the access cookie is used as "
        "the revocation fallback. The endpoint returns no credential values."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        403: {"model": ProblemResponse},
        413: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def staff_logout(
    payload: EmptyStaffRequest,
    request: Request,
    _: Annotated[None, Depends(require_staff_request_boundary)],
    service: Annotated[StaffSessionService, Depends(get_staff_session_service)],
) -> Response:
    del payload
    access_name, refresh_name = staff_cookie_names()
    service.logout(
        request.cookies.get(refresh_name),
        request.cookies.get(access_name),
        correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookies(response, request)
    return response

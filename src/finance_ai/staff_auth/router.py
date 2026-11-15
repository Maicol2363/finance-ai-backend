from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from finance_ai.auth.dependencies import get_session_rate_limiter
from finance_ai.core.errors import ProblemResponse, StaffAccessDeniedError
from finance_ai.core.rate_limit import AuthRateLimiter
from finance_ai.core.roles import MFA_REQUIRED_ROLES
from finance_ai.staff_auth.dependencies import (
    StaffCookieSettings,
    enforce_staff_mfa_limit,
    get_staff_access_user,
    get_staff_recovery_service,
    get_staff_session_service,
    require_staff_cookie_transport,
    require_staff_request_boundary,
    staff_cookie_names,
    staff_mfa_cookie_name,
)
from finance_ai.staff_auth.recovery import StaffRecoveryService
from finance_ai.staff_auth.schemas import (
    StaffLoginCommand,
    StaffLoginRequest,
    StaffMfaChallengeView,
    StaffMfaEnrollmentView,
    StaffMfaResetRequest,
    StaffMfaVerifyRequest,
    StaffRecoveryCodesRequest,
    StaffRecoveryCodesView,
    StaffSessionUser,
    StaffSessionView,
)
from finance_ai.staff_auth.service import StaffMfaChallengeIssued, StaffSessionService

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
    *,
    access: str,
    refresh: str,
    access_expires_at: datetime,
    idle_expires_at: datetime,
    cookie_settings: StaffCookieSettings,
) -> None:
    access_name, refresh_name = staff_cookie_names()
    secure, same_site, http_only = cookie_settings
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


def _set_mfa_cookie(
    response: Response,
    raw_challenge: str,
    expires_at: datetime,
    cookie_settings: StaffCookieSettings,
) -> None:
    secure, same_site, http_only = cookie_settings
    response.set_cookie(
        staff_mfa_cookie_name(),
        raw_challenge,
        max_age=max(0, int((expires_at - datetime.now(UTC)).total_seconds())),
        path="/",
        secure=secure,
        httponly=http_only,
        samesite=same_site,
    )


def _clear_mfa_cookie(response: Response, cookie_settings: StaffCookieSettings) -> None:
    secure, same_site, http_only = cookie_settings
    response.delete_cookie(
        staff_mfa_cookie_name(),
        path="/",
        secure=secure,
        httponly=http_only,
        samesite=same_site,
    )


def _clear_session_cookies(response: Response, cookie_settings: StaffCookieSettings) -> None:
    secure, same_site, http_only = cookie_settings
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
    description=(
        "Verifies the staff password. Support Agents receive a session (200). Accounts whose "
        "role requires SEC-01 MFA (admin, security_auditor) receive 202 with a short-lived, "
        "HttpOnly challenge cookie and no staff authority until TOTP verification succeeds."
    ),
    responses={
        202: {"model": StaffMfaChallengeView},
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
    cookie_settings: Annotated[StaffCookieSettings, Depends(require_staff_cookie_transport)],
    service: Annotated[StaffSessionService, Depends(get_staff_session_service)],
    limiter: Annotated[AuthRateLimiter, Depends(get_session_rate_limiter)],
) -> StaffSessionView | JSONResponse:
    client_key = request.client.host if request.client is not None else "unknown"
    limiter.check_login(client_key=client_key, normalized_email=payload.email.strip().lower())
    outcome = service.login(
        StaffLoginCommand(email=payload.email, password=payload.password.get_secret_value()),
        correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
    )
    if isinstance(outcome, StaffMfaChallengeIssued):
        challenge_response = JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=StaffMfaChallengeView(
                enrollment_required=outcome.enrollment_required,
                challenge_expires_at=outcome.expires_at,
            ).model_dump(mode="json"),
        )
        _set_mfa_cookie(
            challenge_response,
            outcome.raw_challenge,
            outcome.expires_at,
            cookie_settings,
        )
        return challenge_response
    user, access, refresh, access_expires_at, idle_expires_at, absolute_expires_at = outcome
    _set_session_cookies(
        response,
        access=access,
        refresh=refresh,
        access_expires_at=access_expires_at,
        idle_expires_at=idle_expires_at,
        cookie_settings=cookie_settings,
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
    cookie_settings: Annotated[StaffCookieSettings, Depends(require_staff_cookie_transport)],
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
        access=access,
        refresh=refresh,
        access_expires_at=access_expires_at,
        idle_expires_at=idle_expires_at,
        cookie_settings=cookie_settings,
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
    cookie_settings: Annotated[StaffCookieSettings, Depends(require_staff_cookie_transport)],
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
    _clear_session_cookies(response, cookie_settings)
    return response


def _mfa_challenge_cookie(request: Request) -> str | None:
    return request.cookies.get(staff_mfa_cookie_name())


@router.post(
    "/mfa/enrollment",
    response_model=StaffMfaEnrollmentView,
    summary="Start Staff Authenticator Enrollment",
    description=(
        "Inside a valid MFA challenge for an account without an active authenticator, creates "
        "a pending TOTP factor and returns its QR code and secret once. The factor becomes "
        "active only after a valid code is verified."
    ),
    responses={
        401: {"model": ProblemResponse},
        403: {"model": ProblemResponse},
        409: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        429: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def staff_mfa_enrollment(
    payload: EmptyStaffRequest,
    request: Request,
    _: Annotated[None, Depends(require_staff_request_boundary)],
    _transport: Annotated[StaffCookieSettings, Depends(require_staff_cookie_transport)],
    _limit: Annotated[None, Depends(enforce_staff_mfa_limit)],
    service: Annotated[StaffSessionService, Depends(get_staff_session_service)],
) -> StaffMfaEnrollmentView:
    del payload
    enrollment = service.start_mfa_enrollment(
        _mfa_challenge_cookie(request),
        correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
    )
    return StaffMfaEnrollmentView(
        issuer=enrollment.issuer,
        account_name=enrollment.account_name,
        secret=enrollment.secret,
        otpauth_uri=enrollment.otpauth_uri,
        qr_svg_data_url=enrollment.qr_svg_data_url,
    )


@router.post(
    "/mfa/verify",
    response_model=StaffSessionView,
    summary="Verify Staff Authenticator Code",
    description=(
        "Completes password-plus-TOTP sign-in. Rejects invalid, expired and previously "
        "accepted codes; a successful verification issues an MFA-verified staff session."
    ),
    responses={
        401: {"model": ProblemResponse},
        403: {"model": ProblemResponse},
        409: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        429: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def staff_mfa_verify(
    payload: StaffMfaVerifyRequest,
    request: Request,
    response: Response,
    _: Annotated[None, Depends(require_staff_request_boundary)],
    cookie_settings: Annotated[StaffCookieSettings, Depends(require_staff_cookie_transport)],
    _limit: Annotated[None, Depends(enforce_staff_mfa_limit)],
    service: Annotated[StaffSessionService, Depends(get_staff_session_service)],
) -> StaffSessionView:
    user, access, refresh, access_expires_at, idle_expires_at, absolute_expires_at = (
        service.verify_mfa(
            _mfa_challenge_cookie(request),
            payload.code.get_secret_value(),
            correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
        )
    )
    _clear_mfa_cookie(response, cookie_settings)
    _set_session_cookies(
        response,
        access=access,
        refresh=refresh,
        access_expires_at=access_expires_at,
        idle_expires_at=idle_expires_at,
        cookie_settings=cookie_settings,
    )
    return _session_view(user, access_expires_at, idle_expires_at, absolute_expires_at)


@router.post(
    "/mfa/reset",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StaffMfaChallengeView,
    summary="Recover or Replace the Staff Authenticator",
    description=(
        "Verifies the account password plus exactly one proof: a single-use recovery code "
        "(lost device) or the current authenticator code (replacement). On success every "
        "authenticator, recovery code and live session of the account is invalidated and an "
        "enrollment challenge cookie is issued. Failures share one generic response."
    ),
    responses={
        401: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        429: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def staff_mfa_reset(
    payload: StaffMfaResetRequest,
    request: Request,
    _: Annotated[None, Depends(require_staff_request_boundary)],
    cookie_settings: Annotated[StaffCookieSettings, Depends(require_staff_cookie_transport)],
    _limit: Annotated[None, Depends(enforce_staff_mfa_limit)],
    service: Annotated[StaffRecoveryService, Depends(get_staff_recovery_service)],
    limiter: Annotated[AuthRateLimiter, Depends(get_session_rate_limiter)],
) -> JSONResponse:
    client_key = request.client.host if request.client is not None else "unknown"
    limiter.check_login(client_key=client_key, normalized_email=payload.email.strip().lower())
    outcome = service.reset_authenticator(
        email=payload.email,
        password=payload.password.get_secret_value(),
        recovery_code=(
            None if payload.recovery_code is None else payload.recovery_code.get_secret_value()
        ),
        totp_code=None if payload.code is None else payload.code.get_secret_value(),
        correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
    )
    response = JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=StaffMfaChallengeView(
            enrollment_required=outcome.enrollment_required,
            challenge_expires_at=outcome.expires_at,
        ).model_dump(mode="json"),
    )
    _clear_session_cookies(response, cookie_settings)
    _set_mfa_cookie(response, outcome.raw_challenge, outcome.expires_at, cookie_settings)
    return response


@router.post(
    "/mfa/recovery-codes",
    response_model=StaffRecoveryCodesView,
    summary="Issue Staff Recovery Codes",
    description=(
        "For an MFA-verified privileged session, re-confirms the password and replaces the "
        "account's single-use recovery codes. The codes are returned once and stored hashed."
    ),
    responses={
        401: {"model": ProblemResponse},
        403: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def staff_recovery_codes(
    payload: StaffRecoveryCodesRequest,
    request: Request,
    _: Annotated[None, Depends(require_staff_request_boundary)],
    user: Annotated[StaffSessionUser, Depends(get_staff_access_user)],
    service: Annotated[StaffRecoveryService, Depends(get_staff_recovery_service)],
) -> StaffRecoveryCodesView:
    if user.role not in MFA_REQUIRED_ROLES:
        raise StaffAccessDeniedError()
    codes = service.issue_recovery_codes(
        user.id,
        payload.password.get_secret_value(),
        correlation_id=str(getattr(request.state, "correlation_id", "unavailable")),
    )
    return StaffRecoveryCodesView(recovery_codes=codes)


@router.post(
    "/mfa/cancel",
    summary="Cancel Staff MFA Challenge",
    description="Invalidates the current MFA challenge, if any, and clears its cookie.",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        403: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def staff_mfa_cancel(
    payload: EmptyStaffRequest,
    request: Request,
    _: Annotated[None, Depends(require_staff_request_boundary)],
    cookie_settings: Annotated[StaffCookieSettings, Depends(require_staff_cookie_transport)],
    service: Annotated[StaffSessionService, Depends(get_staff_session_service)],
) -> Response:
    del payload
    service.cancel_mfa(_mfa_challenge_cookie(request))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_mfa_cookie(response, cookie_settings)
    return response

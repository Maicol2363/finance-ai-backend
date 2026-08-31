from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from finance_ai.auth.dependencies import (
    enforce_registration_rate_limit,
    get_registration_service,
    get_session_rate_limiter,
    get_session_service,
    require_bearer_access,
)
from finance_ai.auth.schemas import RegisterRequest, RegistrationResponse
from finance_ai.auth.service import RegistrationService
from finance_ai.auth.session_schemas import (
    AuthenticatedUserResponse,
    IssuedSessionResponse,
    LoginRequest,
    RenewableCredentialRequest,
)
from finance_ai.auth.session_service import SessionService
from finance_ai.core.errors import ProblemResponse
from finance_ai.core.rate_limit import AuthRateLimiter

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegistrationResponse,
    responses={
        409: {"model": ProblemResponse},
        413: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        429: {"model": ProblemResponse},
        500: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
    dependencies=[Depends(enforce_registration_rate_limit)],
)
def register(
    payload: RegisterRequest,
    service: Annotated[RegistrationService, Depends(get_registration_service)],
) -> RegistrationResponse:
    result = service.register(payload.to_command())
    return RegistrationResponse.from_result(result)


@router.post(
    "/login",
    response_model=IssuedSessionResponse,
    responses={
        401: {"model": ProblemResponse},
        413: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        429: {"model": ProblemResponse},
        500: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def login(
    payload: LoginRequest,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
    limiter: Annotated[AuthRateLimiter, Depends(get_session_rate_limiter)],
) -> IssuedSessionResponse:
    client_key = request.client.host if request.client is not None else "unknown"
    limiter.check_login(
        client_key=client_key,
        normalized_email=payload.email.strip().lower(),
    )
    return IssuedSessionResponse.from_result(service.login(payload.to_command()))


@router.post(
    "/refresh",
    response_model=IssuedSessionResponse,
    responses={
        401: {"model": ProblemResponse},
        413: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        429: {"model": ProblemResponse},
        500: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def refresh(
    payload: RenewableCredentialRequest,
    request: Request,
    service: Annotated[SessionService, Depends(get_session_service)],
    limiter: Annotated[AuthRateLimiter, Depends(get_session_rate_limiter)],
) -> IssuedSessionResponse:
    client_key = request.client.host if request.client is not None else "unknown"
    limiter.check_refresh(client_key=client_key)
    return IssuedSessionResponse.from_result(service.refresh(payload.raw_credential()))


@router.get(
    "/me",
    response_model=AuthenticatedUserResponse,
    responses={
        401: {"model": ProblemResponse},
        500: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def current_user(
    access_credential: Annotated[str, Depends(require_bearer_access)],
    service: Annotated[SessionService, Depends(get_session_service)],
) -> AuthenticatedUserResponse:
    return AuthenticatedUserResponse.from_result(service.current_user(access_credential))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        413: {"model": ProblemResponse},
        415: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
        500: {"model": ProblemResponse},
        503: {"model": ProblemResponse},
    },
)
def logout(
    payload: RenewableCredentialRequest,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> Response:
    service.logout(payload.raw_credential())
    return Response(status_code=status.HTTP_204_NO_CONTENT)

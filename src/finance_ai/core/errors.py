from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from finance_ai.core.logging import get_logger

logger = get_logger(__name__)


class ProblemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "about:blank"
    title: str
    status: int
    code: str
    detail: str
    correlation_id: str
    field_errors: dict[str, list[str]] | None = None


class AppError(Exception):
    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        field_errors: Mapping[str, list[str]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.field_errors = dict(field_errors) if field_errors else None
        self.headers = dict(headers) if headers else None


class RegistrationValidationError(AppError):
    def __init__(self, field_errors: Mapping[str, list[str]]) -> None:
        super().__init__(
            status=422,
            code="registration_invalid",
            title="Registration details are invalid",
            detail="Please correct the highlighted fields and try again.",
            field_errors=field_errors,
        )


class EmailAlreadyRegisteredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=409,
            code="email_already_registered",
            title="Email is already registered",
            detail="An account already uses this email. Try logging in instead.",
            field_errors={"email": ["This email is already registered."]},
        )


class RegistrationRateLimitedError(AppError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            status=429,
            code="registration_rate_limited",
            title="Too many registration attempts",
            detail="Please wait before trying again.",
            headers={"Retry-After": str(retry_after_seconds)},
        )


class RequestInvalidError(AppError):
    def __init__(self, field_errors: Mapping[str, list[str]]) -> None:
        super().__init__(
            status=422,
            code="request_invalid",
            title="Request details are invalid",
            detail="Please correct the highlighted fields and try again.",
            field_errors=field_errors,
        )


class InvalidCredentialsError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=401,
            code="invalid_credentials",
            title="Login failed",
            detail="Invalid email or password.",
        )


class LoginRateLimitedError(AppError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            status=429,
            code="login_rate_limited",
            title="Too many login attempts",
            detail="Please wait before trying to log in again.",
            headers={"Retry-After": str(retry_after_seconds)},
        )


class SessionInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=401,
            code="session_invalid",
            title="Session is no longer valid",
            detail="Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class AuthenticationRequiredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=401,
            code="authentication_required",
            title="Authentication required",
            detail="Please log in to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class ServiceUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=503,
            code="service_unavailable",
            title="Service temporarily unavailable",
            detail="The service is temporarily unavailable. Please try again later.",
        )


def correlation_id_from(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "unavailable"))


def problem_payload(
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    correlation_id: str,
    field_errors: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return ProblemResponse(
        title=title,
        status=status,
        code=code,
        detail=detail,
        correlation_id=correlation_id,
        field_errors=field_errors,
    ).model_dump(exclude_none=True)


def _validation_field_errors(exc: RequestValidationError) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for item in exc.errors():
        location = item.get("loc", ())
        field = str(location[-1]) if location else "request"
        message = str(item.get("msg", "Invalid value"))
        errors.setdefault(field, []).append(message)
    return errors


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            headers=exc.headers,
            content=problem_payload(
                status=exc.status,
                code=exc.code,
                title=exc.title,
                detail=exc.detail,
                correlation_id=correlation_id_from(request),
                field_errors=exc.field_errors,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path == "/api/v1/auth/register":
            error: AppError = RegistrationValidationError(_validation_field_errors(exc))
        else:
            error = RequestInvalidError(_validation_field_errors(exc))
        return await app_error_handler(request, error)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled request failure",
            extra={
                "correlation_id": correlation_id_from(request),
                "event": "request.unhandled_failure",
                "outcome": "internal_error",
                "status": 500,
            },
        )
        return JSONResponse(
            status_code=500,
            headers={"X-Correlation-ID": correlation_id_from(request)},
            content=problem_payload(
                status=500,
                code="internal_error",
                title="Unexpected error",
                detail="Something went wrong. Please try again later.",
                correlation_id=correlation_id_from(request),
            ),
        )

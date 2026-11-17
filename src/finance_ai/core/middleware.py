import re
import time
from uuid import uuid4

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from finance_ai.core.errors import problem_payload
from finance_ai.core.logging import bind_correlation_id, get_logger
from finance_ai.core.observability import AuthOperation, auth_metrics, registration_metrics

logger = get_logger(__name__)
_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_AUTH_OPERATIONS: dict[tuple[str, str], AuthOperation] = {
    ("POST", "/api/v1/auth/login"): "login",
    ("POST", "/api/v1/auth/refresh"): "refresh",
    ("GET", "/api/v1/auth/me"): "me",
    ("POST", "/api/v1/auth/logout"): "logout",
}
_JSON_AUTH_PATHS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
}
_STAFF_AUTH_PATHS = {
    "/api/v1/staff/auth/login",
    "/api/v1/staff/auth/refresh",
    "/api/v1/staff/auth/logout",
    "/api/v1/staff/auth/mfa/enrollment",
    "/api/v1/staff/auth/mfa/verify",
    "/api/v1/staff/auth/mfa/cancel",
    "/api/v1/staff/auth/mfa/reset",
    "/api/v1/staff/auth/mfa/recovery-codes",
    "/api/v1/staff/auth/activate",
}
_ME_JSON_BODY = {
    ("PATCH", "/api/v1/me/notification-preferences"),
    ("POST", "/api/v1/me/email/verify"),
}
_STAFF_AUDIT_PREFIX = "/api/v1/staff/audit/"
_STAFF_ADMIN_PREFIX = "/api/v1/staff/admin/"


def _is_me_path(path: str) -> bool:
    return path.startswith("/api/v1/me/")


def _is_staff_audit_path(path: str) -> bool:
    return path.startswith(_STAFF_AUDIT_PREFIX)


def _is_staff_admin_path(path: str) -> bool:
    return path.startswith(_STAFF_ADMIN_PREFIX)


def _is_account_path(path: str) -> bool:
    return path == "/api/v1/accounts" or path.startswith("/api/v1/accounts/")


def _is_category_path(path: str) -> bool:
    return path == "/api/v1/categories" or path.startswith("/api/v1/categories/")


def _is_expense_path(path: str) -> bool:
    return path == "/api/v1/expenses" or path.startswith("/api/v1/expenses/")


def _is_history_path(path: str) -> bool:
    return path == "/api/v1/history"


def _is_capture_path(path: str) -> bool:
    return path.startswith("/api/v1/capture/")


def _is_receipt_upload_path(path: str) -> bool:
    return path in {"/api/v1/receipts/proposal", "/api/v1/receipts/scan"} or (
        path.startswith("/api/v1/receipts/proposal/") and path.endswith("/retry")
    )


MAX_RECEIPT_REQUEST_BODY_BYTES = 10 * 1024 * 1024 + 64 * 1024


def _safe_correlation_id(value: str) -> str:
    return value if _CORRELATION_PATTERN.fullmatch(value) else uuid4().hex


class DiagnosticCORSMiddleware(CORSMiddleware):
    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if response.status_code != 400:
            return response

        correlation_id = _safe_correlation_id(request_headers.get("x-correlation-id", ""))
        logger.info(
            "Cross-origin preflight denied",
            extra={
                "event": "cors.preflight_denied",
                "correlation_id": correlation_id,
                "status": 400,
            },
        )
        headers = {
            key: value
            for key, value in response.headers.items()
            if key not in {"content-type", "content-length"}
        }
        headers["x-correlation-id"] = correlation_id
        return JSONResponse(
            status_code=400,
            headers=headers,
            content=problem_payload(
                status=400,
                code="cors_preflight_denied",
                title="Cross-origin request denied",
                detail="This cross-origin request is not allowed.",
                correlation_id=correlation_id,
            ),
        )


class StaffNoStoreMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(
            "/api/v1/staff/auth/"
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_correlation = headers.get(b"x-correlation-id", b"").decode("ascii", "ignore")
        correlation_id = _safe_correlation_id(raw_correlation)
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        async def send_with_staff_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                normalized = {key.lower() for key, _ in response_headers}
                if b"cache-control" not in normalized:
                    response_headers.append((b"cache-control", b"no-store"))
                if b"pragma" not in normalized:
                    response_headers.append((b"pragma", b"no-cache"))
                if b"x-correlation-id" not in normalized:
                    response_headers.append((b"x-correlation-id", correlation_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, send_with_staff_headers)


class RequestBodyTooLargeError(Exception):
    pass


class RequestGuardMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        existing_correlation = scope.get("state", {}).get("correlation_id")
        raw_correlation = headers.get(b"x-correlation-id", b"").decode("ascii", "ignore")
        correlation_id = (
            existing_correlation
            if isinstance(existing_correlation, str)
            and _CORRELATION_PATTERN.fullmatch(existing_correlation)
            else _safe_correlation_id(raw_correlation)
        )
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        bind_correlation_id(correlation_id)

        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        is_registration = path == "/api/v1/auth/register" and method == "POST"
        auth_operation = _AUTH_OPERATIONS.get((method, path))
        guards_json_body = (
            (method == "POST" and (path in _JSON_AUTH_PATHS or path in _STAFF_AUTH_PATHS))
            or (method in {"POST", "PATCH"} and _is_account_path(path))
            or (method in {"POST", "PATCH", "PUT"} and _is_category_path(path))
            or (method == "PATCH" and _is_expense_path(path))
            or (method == "POST" and _is_capture_path(path))
            or (method == "POST" and _is_staff_audit_path(path))
            or (method in {"POST", "PATCH"} and _is_staff_admin_path(path))
            or (method, path) in _ME_JSON_BODY
        )
        is_support_write = method == "POST" and (
            path == "/api/v1/support/tickets"
            or path == "/api/v1/staff/support/tickets"
            or ("/support/tickets/" in path and path.endswith("/messages"))
        )
        is_receipt_upload = method == "POST" and _is_receipt_upload_path(path)
        body_limit = (
            self.max_body_bytes
            if guards_json_body
            else 3 * 1024 * 1024
            if is_support_write
            else MAX_RECEIPT_REQUEST_BODY_BYTES
            if is_receipt_upload
            else None
        )
        started = time.perf_counter()

        if guards_json_body:
            content_type = headers.get(b"content-type", b"").decode("latin-1").split(";", 1)[0]
            if content_type.strip().lower() != "application/json":
                await self._send_problem(
                    scope,
                    receive,
                    send,
                    status=415,
                    code="unsupported_media_type",
                    title="Unsupported media type",
                    detail="This request must use application/json.",
                    correlation_id=correlation_id,
                )
                self._record_completion(
                    is_registration=is_registration,
                    auth_operation=auth_operation,
                    correlation_id=correlation_id,
                    started=started,
                    status_code=415,
                )
                return
        if body_limit is not None:
            content_length = headers.get(b"content-length")
            if content_length is not None:
                try:
                    if int(content_length) > body_limit:
                        await self._payload_too_large(scope, receive, send, correlation_id)
                        self._record_completion(
                            is_registration=is_registration,
                            auth_operation=auth_operation,
                            correlation_id=correlation_id,
                            started=started,
                            status_code=413,
                        )
                        return
                except ValueError:
                    await self._send_problem(
                        scope,
                        receive,
                        send,
                        status=400,
                        code="invalid_content_length",
                        title="Invalid request",
                        detail="The request could not be processed.",
                        correlation_id=correlation_id,
                    )
                    self._record_completion(
                        is_registration=is_registration,
                        auth_operation=auth_operation,
                        correlation_id=correlation_id,
                        started=started,
                        status_code=400,
                    )
                    return

        received_bytes = 0
        body_too_large = False

        async def limited_receive() -> Message:
            nonlocal received_bytes, body_too_large
            message = await receive()
            if body_limit is not None and message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > body_limit:
                    body_too_large = True
                    raise RequestBodyTooLargeError
            return message

        status_code = 500

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            # Multipart parsers may turn a receive-side exception into their own 400.
            # Suppress that response and emit the canonical 413 after parsing unwinds.
            if body_too_large:
                return
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = list(message.get("headers", []))
                if not any(key.lower() == b"x-correlation-id" for key, _ in response_headers):
                    response_headers.append((b"x-correlation-id", correlation_id.encode("ascii")))
                if (
                    path.startswith("/api/v1/auth/")
                    or path.startswith("/api/v1/staff/auth/")
                    or _is_account_path(path)
                    or _is_category_path(path)
                    or _is_expense_path(path)
                    or _is_history_path(path)
                    or _is_capture_path(path)
                    or _is_staff_audit_path(path)
                    or _is_staff_admin_path(path)
                    or _is_me_path(path)
                ):
                    response_headers.append((b"cache-control", b"no-store"))
                    response_headers.append((b"pragma", b"no-cache"))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_context)
            if body_too_large:
                await self._payload_too_large(scope, receive, send, correlation_id)
                status_code = 413
        except RequestBodyTooLargeError:
            if not body_too_large:
                raise
            await self._payload_too_large(scope, receive, send, correlation_id)
            status_code = 413
        finally:
            self._record_completion(
                is_registration=is_registration,
                auth_operation=auth_operation,
                correlation_id=correlation_id,
                started=started,
                status_code=status_code,
            )

    @staticmethod
    def _record_completion(
        *,
        is_registration: bool,
        auth_operation: AuthOperation | None,
        correlation_id: str,
        started: float,
        status_code: int,
    ) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if is_registration:
            registration_metrics.observe(
                status_code=status_code,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
            )
            return
        if auth_operation is not None:
            auth_metrics.observe(
                operation=auth_operation,
                status_code=status_code,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
            )
            return
        logger.info(
            "Request completed",
            extra={
                "correlation_id": correlation_id,
                "duration_ms": duration_ms,
                "event": "request.completed",
                "outcome": "success" if status_code < 400 else "failure",
                "status": status_code,
            },
        )

    async def _payload_too_large(
        self, scope: Scope, receive: Receive, send: Send, correlation_id: str
    ) -> None:
        await self._send_problem(
            scope,
            receive,
            send,
            status=413,
            code="request_too_large",
            title="Request is too large",
            detail="The request exceeds the allowed size.",
            correlation_id=correlation_id,
        )

    @staticmethod
    async def _send_problem(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        correlation_id: str,
    ) -> None:
        response = JSONResponse(
            status_code=status,
            headers={
                "X-Correlation-ID": correlation_id,
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
            content=problem_payload(
                status=status,
                code=code,
                title=title,
                detail=detail,
                correlation_id=correlation_id,
            ),
        )
        await response(scope, receive, send)

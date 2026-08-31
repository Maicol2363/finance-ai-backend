import re
import time
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from finance_ai.core.errors import problem_payload
from finance_ai.core.logging import get_logger
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
        raw_correlation = headers.get(b"x-correlation-id", b"").decode("ascii", "ignore")
        correlation_id = (
            raw_correlation if _CORRELATION_PATTERN.fullmatch(raw_correlation) else uuid4().hex
        )
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        is_registration = path == "/api/v1/auth/register" and method == "POST"
        auth_operation = _AUTH_OPERATIONS.get((method, path))
        guards_json_body = method == "POST" and path in _JSON_AUTH_PATHS
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
                    detail="Authentication requests must use application/json.",
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
            content_length = headers.get(b"content-length")
            if content_length is not None:
                try:
                    if int(content_length) > self.max_body_bytes:
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

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if guards_json_body and message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    raise RequestBodyTooLargeError
            return message

        status_code = 500

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-correlation-id", correlation_id.encode("ascii")))
                if path.startswith("/api/v1/auth/"):
                    response_headers.append((b"cache-control", b"no-store"))
                    response_headers.append((b"pragma", b"no-cache"))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_context)
        except RequestBodyTooLargeError:
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
            detail="The authentication request exceeds the allowed size.",
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

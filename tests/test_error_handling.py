import asyncio
import logging
from tempfile import SpooledTemporaryFile
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from finance_ai.core.errors import install_exception_handlers
from finance_ai.core.middleware import RequestGuardMiddleware


def test_unhandled_failure_is_safe_and_correlated(caplog: logging.LogCaptureFixture) -> None:
    app = FastAPI()

    @app.get("/test/unhandled-failure")
    def unhandled_failure() -> None:
        raise RuntimeError("private-body-and-stack-sentinel")

    install_exception_handlers(app)
    app.add_middleware(RequestGuardMiddleware, max_body_bytes=1024)
    correlation_id = "safe-correlation-123"

    with caplog.at_level(logging.ERROR, logger="finance_ai.core.errors"):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/test/unhandled-failure",
            headers={"X-Correlation-ID": correlation_id},
        )

    assert response.status_code == 500
    assert response.headers["X-Correlation-ID"] == correlation_id
    assert response.json()["code"] == "internal_error"
    assert response.json()["correlation_id"] == correlation_id
    assert "private-body-and-stack-sentinel" not in response.text
    assert len(caplog.records) == 1
    assert caplog.records[0].correlation_id == correlation_id
    assert caplog.records[0].error_type == "RuntimeError"
    assert caplog.records[0].exc_info is None
    assert "private-body-and-stack-sentinel" not in caplog.text


def test_chunked_receipt_upload_is_bounded_without_content_length() -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"78901", "more_body": True},
        ]
    )
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return next(messages)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def consume_body(scope, receive, send) -> None:
        while (await receive()).get("more_body"):
            pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/receipts/scan",
        "raw_path": b"/api/v1/receipts/scan",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"multipart/form-data; boundary=test")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "state": {},
    }
    middleware = RequestGuardMiddleware(consume_body, max_body_bytes=1024)

    with patch("finance_ai.core.middleware.MAX_RECEIPT_REQUEST_BODY_BYTES", 10):
        asyncio.run(middleware(scope, receive, send))

    response_start = next(message for message in sent if message["type"] == "http.response.start")
    assert response_start["status"] == 413


def test_rejected_chunked_multipart_closes_unrolled_spooled_file() -> None:
    boundary = b"parser-boundary"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="receipt.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n" + b"x" * 512 + b"\r\n--" + boundary + b"--\r\n"
    )
    chunks = iter(body[index : index + 32] for index in range(0, len(body), 32))
    spooled_files: list[SpooledTemporaryFile[bytes]] = []

    def create_spooled_file(*args, **kwargs) -> SpooledTemporaryFile[bytes]:
        # Retain the handle solely to assert parser cleanup after the body limit trips.
        file = SpooledTemporaryFile(*args, **kwargs)  # noqa: SIM115
        spooled_files.append(file)
        return file

    async def receive() -> dict[str, object]:
        try:
            chunk = next(chunks)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def parse_multipart(scope, receive, send) -> None:
        request = Request(scope, receive=receive)
        await request.form()

    sent: list[dict[str, object]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/receipts/proposal",
        "raw_path": b"/api/v1/receipts/proposal",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"multipart/form-data; boundary=" + boundary)],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "state": {},
    }
    middleware = RequestGuardMiddleware(parse_multipart, max_body_bytes=10_000)

    with (
        patch("finance_ai.core.middleware.MAX_RECEIPT_REQUEST_BODY_BYTES", 192),
        patch("starlette.formparsers.SpooledTemporaryFile", side_effect=create_spooled_file),
    ):
        asyncio.run(middleware(scope, receive, send))

    response_start = next(message for message in sent if message["type"] == "http.response.start")
    assert response_start["status"] == 413
    assert len(spooled_files) == 1
    assert spooled_files[0].closed
    assert not spooled_files[0]._rolled

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

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

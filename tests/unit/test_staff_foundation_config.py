import json

from fastapi.testclient import TestClient

from finance_ai.core.config import get_settings
from finance_ai.main import create_app


def test_staff_web_cors_allows_configured_origin_without_credentials(monkeypatch) -> None:
    origin = "http://127.0.0.1:5173"
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", json.dumps([origin]))
    get_settings.cache_clear()
    try:
        response = TestClient(create_app()).options(
            "/api/v1/auth/login",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,x-correlation-id",
            },
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in response.headers


def test_staff_web_cors_does_not_reflect_an_unconfigured_origin(monkeypatch) -> None:
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", '["http://127.0.0.1:5173"]')
    get_settings.cache_clear()
    try:
        response = TestClient(create_app()).options(
            "/api/v1/auth/login",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_staff_web_cors_covers_request_guard_unsupported_media_response(monkeypatch) -> None:
    origin = "http://127.0.0.1:5173"
    correlation_id = "guard-unsupported-media"
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", json.dumps([origin]))
    get_settings.cache_clear()
    try:
        response = TestClient(create_app()).post(
            "/api/v1/auth/login",
            content='{"email":"test@example.com"}',
            headers={
                "Origin": origin,
                "Content-Type": "text/plain",
                "X-Correlation-ID": correlation_id,
            },
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 415
    assert response.headers["access-control-allow-origin"] == origin
    assert "x-correlation-id" in response.headers["access-control-expose-headers"].lower()
    assert response.headers["x-correlation-id"] == correlation_id
    assert response.json()["correlation_id"] == correlation_id


def test_staff_web_cors_covers_request_guard_oversized_body_response(monkeypatch) -> None:
    origin = "http://127.0.0.1:5173"
    correlation_id = "guard-body-too-large"
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", json.dumps([origin]))
    monkeypatch.setenv("FINANCE_AI_MAX_REQUEST_BODY_BYTES", "1024")
    get_settings.cache_clear()
    try:
        response = TestClient(create_app()).post(
            "/api/v1/auth/login",
            content='{"email":"' + "x" * 1100 + '"}',
            headers={
                "Origin": origin,
                "Content-Type": "application/json",
                "X-Correlation-ID": correlation_id,
            },
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == origin
    assert "x-correlation-id" in response.headers["access-control-expose-headers"].lower()
    assert response.headers["x-correlation-id"] == correlation_id
    assert response.json()["correlation_id"] == correlation_id

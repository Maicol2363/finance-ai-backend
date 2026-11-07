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

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
from starlette.requests import Request

from finance_ai.core.config import get_settings
from finance_ai.core.errors import StaffOriginDeniedError, StaffRequestDeniedError
from finance_ai.staff_auth.dependencies import (
    _is_loopback_host,
    get_staff_access_user,
    require_staff_request_boundary,
    staff_cookie_names,
    staff_cookie_settings,
)

ORIGIN = "https://staff.example.test"


def request(
    *,
    method: str = "POST",
    url: str = ORIGIN,
    headers: dict[str, str] | None = None,
) -> Request:
    parsed_url = urlsplit(url)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": parsed_url.scheme,
        "path": "/api/v1/staff/auth/session",
        "raw_path": b"/api/v1/staff/auth/session",
        "query_string": b"",
        "root_path": "",
        "server": (
            parsed_url.hostname,
            parsed_url.port or (443 if url.startswith("https") else 80),
        ),
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
        ],
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def clear_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", json.dumps([ORIGIN]))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_boundary_requires_custom_header_and_blocks_bearer_and_cross_site() -> None:
    with pytest.raises(StaffRequestDeniedError):
        require_staff_request_boundary(request(headers={"Origin": ORIGIN}))
    with pytest.raises(StaffRequestDeniedError):
        require_staff_request_boundary(
            request(headers={"Origin": ORIGIN, "X-Staff-Request": "1", "Authorization": "Bearer x"})
        )
    with pytest.raises(StaffOriginDeniedError):
        require_staff_request_boundary(
            request(
                headers={
                    "Origin": ORIGIN,
                    "X-Staff-Request": "1",
                    "Sec-Fetch-Site": "cross-site",
                }
            )
        )


def test_boundary_allows_same_origin_get_without_origin_but_denies_missing_fetch_metadata() -> None:
    require_staff_request_boundary(
        request(method="GET", headers={"X-Staff-Request": "1", "Sec-Fetch-Site": "same-origin"})
    )
    with pytest.raises(StaffOriginDeniedError):
        require_staff_request_boundary(request(method="GET", headers={"X-Staff-Request": "1"}))


def test_boundary_allows_explicit_loopback_http_only_when_local_exception_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = "http://127.0.0.1:5173"
    monkeypatch.setenv("FINANCE_AI_CORS_ALLOWED_ORIGINS", json.dumps([origin]))
    monkeypatch.setenv("FINANCE_AI_STAFF_LOCAL_COOKIE_EXCEPTION_ENABLED", "true")
    get_settings.cache_clear()
    require_staff_request_boundary(
        request(url=origin, headers={"Origin": origin, "X-Staff-Request": "1"})
    )
    with pytest.raises(StaffOriginDeniedError):
        require_staff_request_boundary(
            request(
                url="http://staff.example.test:5173",
                headers={"Origin": "http://staff.example.test:5173", "X-Staff-Request": "1"},
            )
        )


def test_cookie_names_and_settings_follow_environment_and_loopback_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert staff_cookie_names() == ("finance_staff_access", "finance_staff_refresh")
    assert staff_cookie_settings(request(url="https://staff.example.test")) == (
        True,
        "strict",
        True,
    )
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "FINANCE_AI_DATABASE_URL",
        "postgresql+psycopg://finance_ai:test-only@localhost:5432/finance_ai",
    )
    monkeypatch.setenv("FINANCE_AI_SESSION_LIMITER_SECRET", "test-only-production-secret")
    get_settings.cache_clear()
    assert staff_cookie_names() == (
        "__Host-finance_staff_access",
        "__Host-finance_staff_refresh",
    )
    with pytest.raises(StaffOriginDeniedError):
        staff_cookie_settings(request(url="http://localhost:8000"))
    monkeypatch.setenv("FINANCE_AI_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCE_AI_STAFF_LOCAL_COOKIE_EXCEPTION_ENABLED", "true")
    get_settings.cache_clear()
    assert staff_cookie_settings(request(url="http://localhost:8000")) == (False, "strict", True)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_loopback_host_recognizes_only_loopback_addresses(host: str) -> None:
    assert _is_loopback_host(host)
    assert not _is_loopback_host("staff.example.test")
    assert not _is_loopback_host(None)


def test_access_user_reads_only_staff_access_cookie() -> None:
    class ServiceStub:
        def resolve(self, raw_access: str | None, *, correlation_id: str):
            return raw_access, correlation_id

    req = request(headers={"Cookie": "finance_staff_access=token"})
    req.state.correlation_id = "request-correlation"
    user = get_staff_access_user(req, None, ServiceStub())  # type: ignore[arg-type]
    assert user == ("token", "request-correlation")

from fastapi.testclient import TestClient

from finance_ai.main import create_app


def test_security_headers_present_on_successful_responses() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert response.headers.get("x-frame-options") == "DENY"


def test_security_headers_present_on_middleware_error_responses() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/register",
            content="invalid payload format",
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 415
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert response.headers.get("x-frame-options") == "DENY"

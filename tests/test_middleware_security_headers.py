from fastapi.testclient import TestClient

from finance_ai.main import create_app


def test_security_headers_present_on_normal_responses() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"


def test_security_headers_present_on_middleware_problem_responses() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            content=b"not-json",
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 415
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"

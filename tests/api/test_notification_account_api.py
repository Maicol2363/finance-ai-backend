"""The account-facing notification endpoints and the public unsubscribe page (EML-02)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from finance_ai.accounts.dependencies import get_authenticated_principal
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.config import Settings, get_settings
from finance_ai.main import create_app
from finance_ai.notifications.dependencies import get_notification_account_service
from finance_ai.notifications.tokens import make_unsubscribe_token
from tests.notification_support import ANA, ANA_ADDRESS, VERIFIED_USER, Env, build_env

PREFERENCES = "/api/v1/me/notification-preferences"
FULLWIDTH = "".join(chr(0xFF11 + offset) for offset in range(6))  # looks like digits, is not 0-9
TRAILING_NEWLINE = "123456" + chr(10)


def make_client(env: Env, *, signed_in_as: int | None = ANA) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: env.settings
    app.dependency_overrides[get_notification_account_service] = lambda: env.service
    if signed_in_as is not None:
        principal = PublicAuthenticatedUser(
            user_id=signed_in_as, email=ANA_ADDRESS, preferred_currency="COP"
        )
        app.dependency_overrides[get_authenticated_principal] = lambda: principal
    return TestClient(app)


@pytest.fixture
def env() -> Env:
    return build_env()


@pytest.fixture
def client(env: Env) -> Iterator[TestClient]:
    yield make_client(env)


# --- who may call --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", PREFERENCES),
        ("PATCH", PREFERENCES),
        ("POST", "/api/v1/me/email/verification"),
        ("POST", "/api/v1/me/email/verify"),
        ("POST", "/api/v1/me/notifications/test"),
    ],
)
def test_every_endpoint_requires_a_signed_in_person(env: Env, method: str, path: str) -> None:
    http = make_client(env, signed_in_as=None)

    response = http.request(method, path, json={"email_enabled": False, "code": "123456"})

    assert response.status_code == 401


# --- preferences ---------------------------------------------------------------------------


def test_the_preferences_never_contain_the_full_address(client: TestClient) -> None:
    response = client.get(PREFERENCES)

    assert response.status_code == 200
    assert response.json() == {
        "email_hint": "a***@example.com",
        "email_verified": False,
        "email_enabled": False,
        "budget_alerts": True,
        "show_amounts": True,
        "delivery_available": True,
    }
    assert ANA_ADDRESS not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_turning_email_on_before_verifying_is_a_409(client: TestClient) -> None:
    response = client.patch(PREFERENCES, json={"email_enabled": True})

    assert response.status_code == 409 and response.json()["code"] == "email_not_verified"


def test_a_verified_person_changes_only_what_they_send(env: Env) -> None:
    http = make_client(env, signed_in_as=VERIFIED_USER)

    response = http.patch(PREFERENCES, json={"email_enabled": True, "show_amounts": False})

    assert response.status_code == 200
    body = response.json()
    assert (body["email_enabled"], body["budget_alerts"], body["show_amounts"]) == (
        True,
        True,
        False,
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"email_enabled": None},
        {"email_enabled": "yes"},
        {"unknown": True},
        {"budget_alerts": 5},
    ],
)
def test_malformed_preference_updates_are_rejected(client: TestClient, body: object) -> None:
    response = client.patch(PREFERENCES, json=body)

    assert response.status_code == 422


def test_preference_updates_must_be_json(client: TestClient) -> None:
    response = client.patch(
        PREFERENCES,
        content="email_enabled=true",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 415


# --- verification --------------------------------------------------------------------------


def test_the_whole_opt_in_flow_over_http(env: Env, client: TestClient) -> None:
    asked = client.post("/api/v1/me/email/verification")
    code = env.sender.last_code()
    verified = client.post("/api/v1/me/email/verify", json={"code": code})

    assert asked.status_code == 200
    assert asked.json() == {
        "email_hint": "a***@example.com",
        "expires_in_seconds": 900,
        "resend_in_seconds": 60,
    }
    assert code not in asked.text
    assert verified.status_code == 200
    assert verified.json()["email_verified"] is True and verified.json()["email_enabled"] is True


def test_asking_for_a_second_code_too_soon_returns_retry_after(client: TestClient) -> None:
    client.post("/api/v1/me/email/verification")

    response = client.post("/api/v1/me/email/verification")

    assert response.status_code == 429
    assert response.json()["code"] == "verification_cooldown"
    assert int(response.headers["retry-after"]) >= 1


def test_a_wrong_code_is_a_400_and_a_badly_shaped_one_a_422(env: Env, client: TestClient) -> None:
    client.post("/api/v1/me/email/verification")
    real = env.sender.last_code()
    wrong_code = "000000" if real != "000000" else "111111"

    wrong = client.post("/api/v1/me/email/verify", json={"code": wrong_code})
    shapes = [
        client.post("/api/v1/me/email/verify", json={"code": bad})
        for bad in ("12345", "1234567", "12345a", FULLWIDTH, " 12345", "", TRAILING_NEWLINE)
    ]

    assert wrong.status_code == 400 and wrong.json()["code"] == "verification_code_invalid"
    assert all(response.status_code == 422 for response in shapes)


def test_the_verify_endpoint_requires_json(client: TestClient) -> None:
    response = client.post(
        "/api/v1/me/email/verify",
        content="code=123456",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 415


def test_a_test_email_over_http(env: Env) -> None:
    http = make_client(env, signed_in_as=VERIFIED_USER)

    response = http.post("/api/v1/me/notifications/test")

    assert response.status_code == 200 and response.json() == {"status": "sent"}
    assert env.sender.sent[0].to_address == "marta@example.com"


def test_a_test_email_for_an_unverified_address_is_a_409(client: TestClient) -> None:
    response = client.post("/api/v1/me/notifications/test")

    assert response.status_code == 409 and response.json()["code"] == "email_not_verified"


def test_the_endpoints_are_documented_in_the_public_contract(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert {PREFERENCES, "/api/v1/me/email/verification", "/api/v1/me/email/verify"} <= set(paths)
    assert "/api/v1/me/notifications/test" in paths
    assert not any("unsubscribe" in path for path in paths)  # the public page stays out of it


# --- the unsubscribe page ------------------------------------------------------------------


def opted_in_env() -> Env:
    env = build_env()
    env.service.update_preferences(
        VERIFIED_USER, email_enabled=True, budget_alerts=None, show_amounts=None
    )
    return env


def test_opening_the_link_changes_nothing_and_asks_for_confirmation() -> None:
    env = opted_in_env()
    http = make_client(env, signed_in_as=None)
    token = make_unsubscribe_token(env.settings, VERIFIED_USER)

    page = http.get("/public/unsubscribe", params={"token": token})

    assert page.status_code == 200 and "<form" in page.text and 'method="post"' in page.text
    assert env.service.get_preferences(VERIFIED_USER).email_enabled is True


def test_confirming_turns_email_off_and_repeating_is_harmless() -> None:
    env = opted_in_env()
    http = make_client(env, signed_in_as=None)
    token = make_unsubscribe_token(env.settings, VERIFIED_USER)

    first = http.post("/public/unsubscribe", params={"token": token})
    again = http.post("/public/unsubscribe", params={"token": token})

    assert first.status_code == again.status_code == 200
    assert env.service.get_preferences(VERIFIED_USER).email_enabled is False


def test_a_mail_clients_one_click_post_works_without_signing_in() -> None:
    env = opted_in_env()
    http = make_client(env, signed_in_as=None)
    token = make_unsubscribe_token(env.settings, VERIFIED_USER)

    response = http.post(
        f"/public/unsubscribe?token={token}", data={"List-Unsubscribe": "One-Click"}
    )

    assert response.status_code == 200
    assert env.service.get_preferences(VERIFIED_USER).email_enabled is False


@pytest.mark.parametrize(
    "token",
    [
        "",
        "garbage",
        "v1.3.",
        "v1.3." + "A" * 43,  # right shape, wrong signature
        "v2.3." + "A" * 43,
        "v1.0." + "A" * 43,
        "v1.-3." + "A" * 43,
    ],
)
def test_a_forged_or_broken_token_is_refused_and_changes_nothing(token: str) -> None:
    env = opted_in_env()
    http = make_client(env, signed_in_as=None)

    shown = http.get("/public/unsubscribe", params={"token": token})
    posted = http.post("/public/unsubscribe", params={"token": token})

    assert shown.status_code == posted.status_code == 400
    assert env.service.get_preferences(VERIFIED_USER).email_enabled is True


def test_a_token_for_one_person_cannot_be_edited_to_switch_off_another() -> None:
    env = opted_in_env()
    http = make_client(env, signed_in_as=None)
    token = make_unsubscribe_token(env.settings, 2)  # someone else's genuine token
    forged = token.replace(".2.", f".{VERIFIED_USER}.", 1)

    response = http.post("/public/unsubscribe", params={"token": forged})

    assert response.status_code == 400
    assert env.service.get_preferences(VERIFIED_USER).email_enabled is True


def test_a_token_signed_with_another_secret_is_refused() -> None:
    env = opted_in_env()
    http = make_client(env, signed_in_as=None)
    other = Settings(_env_file=None, session_limiter_secret="a-completely-different-secret-1")

    response = http.post(
        "/public/unsubscribe", params={"token": make_unsubscribe_token(other, VERIFIED_USER)}
    )

    assert response.status_code == 400


def test_a_valid_token_for_a_deleted_account_gets_the_same_quiet_answer() -> None:
    env = build_env()
    http = make_client(env, signed_in_as=None)

    response = http.post(
        "/public/unsubscribe", params={"token": make_unsubscribe_token(env.settings, 999)}
    )

    assert response.status_code == 200  # nothing to reveal about who exists


def test_the_page_is_not_cached_indexed_or_leaked_through_the_referrer() -> None:
    env = opted_in_env()
    http = make_client(env, signed_in_as=None)
    token = make_unsubscribe_token(env.settings, VERIFIED_USER)

    page = http.get("/public/unsubscribe", params={"token": token})

    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in page.headers["x-robots-tag"]
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "<script" not in page.text


def test_a_token_in_the_page_cannot_break_out_of_the_form_action() -> None:
    env = opted_in_env()
    http = make_client(env, signed_in_as=None)

    page = http.get("/public/unsubscribe", params={"token": '"><script>alert(1)</script>'})

    assert page.status_code == 400 and "<script>alert" not in page.text

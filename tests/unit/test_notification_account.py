"""EML-02 behaviour: verified opt-in by a six-digit code, preferences, test email, unsubscribe."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from finance_ai.core.config import Settings
from finance_ai.core.errors import AppError
from finance_ai.db.models.notifications import (
    EmailVerification,
    NotificationOutbox,
    NotificationPreference,
)
from finance_ai.notifications.account import mask_email
from finance_ai.notifications.messages import EmailSendError
from finance_ai.notifications.outbox import enqueue
from finance_ai.notifications.senders import build_mime
from tests.notification_support import (
    ANA,
    ANA_ADDRESS,
    LUIS,
    VERIFIED_USER,
    Env,
    build_env,
)


@pytest.fixture
def env() -> Env:
    return build_env()


@contextmanager
def fails_with(code: str, status: int | None = None) -> Iterator[None]:
    with pytest.raises(AppError) as caught:
        yield
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status == status


def count(env: Env, model: Any, *where: Any) -> int:
    with env.factory() as session:
        return int(session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def verification_rows(env: Env, user_id: int) -> list[EmailVerification]:
    with env.factory() as session:
        return list(
            session.scalars(select(EmailVerification).where(EmailVerification.user_id == user_id))
        )


# --- preferences ---------------------------------------------------------------------------


def test_a_person_without_saved_choices_starts_with_email_off_and_alerts_on(env: Env) -> None:
    view = env.service.get_preferences(ANA)

    assert (view.email_enabled, view.budget_alerts, view.show_amounts) == (False, True, True)
    assert view.email_verified is False
    assert view.delivery_available is True


def test_the_address_is_only_ever_shown_masked(env: Env) -> None:
    view = env.service.get_preferences(ANA)

    assert view.email_hint == "a***@example.com"
    assert ANA_ADDRESS not in repr(view)
    assert mask_email("sin-arroba") == "***"
    assert mask_email("@example.com") == "***"


def test_email_cannot_be_turned_on_before_the_address_is_verified(env: Env) -> None:
    with fails_with("email_not_verified", 409):
        env.service.update_preferences(
            ANA, email_enabled=True, budget_alerts=None, show_amounts=None
        )

    assert env.service.get_preferences(ANA).email_enabled is False


def test_a_verified_person_can_turn_email_on_and_choose_what_to_receive(env: Env) -> None:
    view = env.service.update_preferences(
        VERIFIED_USER, email_enabled=True, budget_alerts=False, show_amounts=False
    )

    assert (view.email_enabled, view.budget_alerts, view.show_amounts) == (True, False, False)


def test_turning_things_off_is_always_allowed_and_changes_only_what_was_sent(env: Env) -> None:
    env.service.update_preferences(
        VERIFIED_USER, email_enabled=True, budget_alerts=None, show_amounts=None
    )

    view = env.service.update_preferences(
        VERIFIED_USER, email_enabled=None, budget_alerts=None, show_amounts=False
    )
    off = env.service.update_preferences(
        ANA, email_enabled=False, budget_alerts=None, show_amounts=None
    )

    assert (view.email_enabled, view.budget_alerts, view.show_amounts) == (True, True, False)
    assert off.email_enabled is False  # unverified people can still switch things off


def test_an_unknown_account_is_reported_not_created(env: Env) -> None:
    with fails_with("account_not_found", 404):
        env.service.get_preferences(999)

    assert count(env, NotificationPreference) == 0


# --- requesting a code ---------------------------------------------------------------------


def test_requesting_a_code_emails_it_and_stores_only_a_hash(env: Env) -> None:
    issued = env.service.request_verification(ANA)

    assert len(env.sender.sent) == 1
    message = env.sender.sent[0]
    code = env.sender.last_code()
    assert message.to_address == ANA_ADDRESS
    assert (
        code in message.text_body.replace(" ", "") and f"{code[:3]} {code[3:]}" in message.html_body
    )
    # build_mime marks every message auto-generated; the caller must not add a second header.
    assert "Auto-Submitted" not in message.headers
    assert build_mime(message)[0].get_all("Auto-Submitted") == ["auto-generated"]
    assert (issued.email_hint, issued.expires_in_seconds, issued.resend_in_seconds) == (
        "a***@example.com",
        15 * 60,
        60,
    )
    [row] = verification_rows(env, ANA)
    assert code not in row.code_hash and len(row.code_hash) == 64
    assert row.expires_at.replace(tzinfo=UTC) == env.clock() + timedelta(minutes=15)
    assert count(env, NotificationOutbox) == 0  # the code never goes through the outbox


def test_the_same_code_hashes_differently_for_another_person(env: Env) -> None:
    from finance_ai.notifications.tokens import hash_verification_code

    first = hash_verification_code(env.settings, user_id=ANA, code="123456")
    second = hash_verification_code(env.settings, user_id=LUIS, code="123456")

    assert first != second


def test_asking_again_too_soon_is_refused_with_the_time_to_wait(env: Env) -> None:
    env.service.request_verification(ANA)
    env.clock.advance(seconds=20)

    with pytest.raises(AppError) as refused:
        env.service.request_verification(ANA)

    assert refused.value.status == 429 and refused.value.code == "verification_cooldown"
    assert refused.value.headers == {"Retry-After": "40"}
    assert len(env.sender.sent) == 1


def test_a_new_code_replaces_the_previous_one(env: Env) -> None:
    env.service.request_verification(ANA)
    first = env.sender.last_code()
    env.clock.advance(seconds=61)

    env.service.request_verification(ANA)
    second = env.sender.last_code()

    open_rows = [row for row in verification_rows(env, ANA) if row.consumed_at is None]
    assert len(open_rows) == 1
    if first != second:  # one in a million they coincide; then the old code is the new one
        with fails_with("verification_code_invalid", 400):
            env.service.confirm_verification(ANA, first)
    assert env.service.confirm_verification(ANA, second).email_verified is True


def test_at_most_five_codes_an_hour(env: Env) -> None:
    for _ in range(5):
        env.service.request_verification(ANA)
        env.clock.advance(seconds=61)

    with fails_with("verification_rate_limited", 429):
        env.service.request_verification(ANA)

    env.clock.advance(hours=1)
    env.service.request_verification(ANA)  # the hour has passed
    assert len(env.sender.sent) == 6


def test_a_delivery_failure_is_reported_and_does_not_burn_the_limits(env: Env) -> None:
    env.sender.failure = EmailSendError("provider_unavailable", retryable=True)

    with fails_with("email_delivery_failed", 503):
        env.service.request_verification(ANA)

    assert verification_rows(env, ANA) == []
    env.sender.failure = None
    env.service.request_verification(ANA)  # no cooldown: the first code never left
    assert len(env.sender.sent) == 1


def test_a_failed_resend_leaves_the_earlier_code_in_the_inbox_usable(env: Env) -> None:
    env.service.request_verification(ANA)
    first = env.sender.last_code()
    env.clock.advance(seconds=61)
    env.sender.failure = EmailSendError("provider_unavailable", retryable=True)

    with fails_with("email_delivery_failed", 503):
        env.service.request_verification(ANA)

    env.sender.failure = None
    assert env.service.confirm_verification(ANA, first).email_verified is True


def test_a_failed_resend_does_not_reopen_an_earlier_code_that_has_expired(env: Env) -> None:
    env.service.request_verification(ANA)
    first = env.sender.last_code()
    env.clock.advance(minutes=16)
    env.sender.failure = EmailSendError("provider_unavailable", retryable=True)

    with fails_with("email_delivery_failed", 503):
        env.service.request_verification(ANA)

    env.sender.failure = None
    with fails_with("verification_code_invalid", 400):
        env.service.confirm_verification(ANA, first)


def test_an_already_verified_address_does_not_get_another_code(env: Env) -> None:
    with fails_with("email_already_verified", 409):
        env.service.request_verification(VERIFIED_USER)

    assert env.sender.sent == []


def test_without_a_real_sender_in_production_everything_reports_unavailable() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+psycopg://u:p@db.example.com/finance_ai",
        session_limiter_secret="a-production-limiter-secret-value-1",
    )
    env = build_env(settings)

    with fails_with("notifications_unavailable", 503):
        env.service.request_verification(ANA)
    with fails_with("notifications_unavailable", 503):
        env.service.send_test_email(VERIFIED_USER)
    with fails_with("notifications_unavailable", 503):
        env.service.update_preferences(
            VERIFIED_USER, email_enabled=True, budget_alerts=None, show_amounts=None
        )
    assert env.service.get_preferences(ANA).delivery_available is False


# --- confirming a code ---------------------------------------------------------------------


def test_the_right_code_verifies_the_address_and_turns_email_on(env: Env) -> None:
    env.service.request_verification(ANA)

    view = env.service.confirm_verification(ANA, env.sender.last_code())

    assert view.email_verified is True and view.email_enabled is True
    assert all(row.consumed_at is not None for row in verification_rows(env, ANA))


def test_a_code_works_only_once(env: Env) -> None:
    env.service.request_verification(ANA)
    code = env.sender.last_code()
    env.service.confirm_verification(ANA, code)

    with fails_with("verification_code_invalid", 400):
        env.service.confirm_verification(ANA, code)


def test_a_wrong_code_counts_an_attempt_and_five_burn_the_code(env: Env) -> None:
    env.service.request_verification(ANA)
    real = env.sender.last_code()
    wrong = "000000" if real != "000000" else "111111"

    for _ in range(4):
        with fails_with("verification_code_invalid", 400):
            env.service.confirm_verification(ANA, wrong)
    assert verification_rows(env, ANA)[0].attempts == 4  # persisted although each call failed
    with fails_with("verification_attempts_exceeded", 429):
        env.service.confirm_verification(ANA, wrong)

    with fails_with("verification_code_invalid", 400):  # even the right code is dead now
        env.service.confirm_verification(ANA, real)
    assert env.service.get_preferences(ANA).email_verified is False


def test_an_expired_code_is_refused_and_closed(env: Env) -> None:
    env.service.request_verification(ANA)
    code = env.sender.last_code()
    env.clock.advance(minutes=16)

    with fails_with("verification_code_expired", 400):
        env.service.confirm_verification(ANA, code)

    with fails_with("verification_code_invalid", 400):
        env.service.confirm_verification(ANA, code)


def test_confirming_without_asking_for_a_code_is_refused(env: Env) -> None:
    with fails_with("verification_code_invalid", 400):
        env.service.confirm_verification(ANA, "123456")


def test_a_code_cannot_be_used_by_someone_else(env: Env) -> None:
    env.service.request_verification(ANA)
    anas_code = env.sender.last_code()
    env.service.request_verification(LUIS)

    with fails_with("verification_code_invalid", 400):
        env.service.confirm_verification(LUIS, anas_code)

    assert env.service.get_preferences(LUIS).email_verified is False


# --- test email ----------------------------------------------------------------------------


def test_a_test_email_needs_a_verified_address(env: Env) -> None:
    with fails_with("email_not_verified", 409):
        env.service.send_test_email(ANA)

    assert env.sender.sent == []


def test_a_test_email_is_sent_right_away_to_the_person(env: Env) -> None:
    result = env.service.send_test_email(VERIFIED_USER)

    assert result.status == "sent"
    [message] = env.sender.sent
    assert message.to_address == "marta@example.com"
    assert message.subject == "Correo de prueba de Finance AI"


def test_test_emails_are_limited_to_three_an_hour(env: Env) -> None:
    for _ in range(3):
        assert env.service.send_test_email(VERIFIED_USER).status == "sent"

    with pytest.raises(AppError) as refused:
        env.service.send_test_email(VERIFIED_USER)

    assert refused.value.status == 429 and refused.value.code == "test_email_rate_limited"
    assert len(env.sender.sent) == 3


def test_asking_for_a_test_does_not_send_other_peoples_waiting_notifications(env: Env) -> None:
    with env.factory() as session, session.begin():
        enqueue(
            session,
            user_id=ANA,
            kind="test",
            dedupe_key="someone-elses",
            payload={},
            not_before=env.clock() - timedelta(minutes=1),
        )

    env.service.send_test_email(VERIFIED_USER)

    assert [m.to_address for m in env.sender.sent] == ["marta@example.com"]
    with env.factory() as session:
        status = session.scalar(
            select(NotificationOutbox.status).where(
                NotificationOutbox.dedupe_key == "someone-elses"
            )
        )
    assert status == "pending"


def test_a_test_email_that_cannot_be_delivered_now_is_queued_for_the_retry(env: Env) -> None:
    env.sender.failure = EmailSendError("provider_unavailable", retryable=True)

    assert env.service.send_test_email(VERIFIED_USER).status == "queued"


def test_a_test_email_that_fails_for_good_is_reported_not_called_queued(env: Env) -> None:
    env.sender.failure = EmailSendError("provider_auth_failed", retryable=False)

    with fails_with("email_delivery_failed", 503):
        env.service.send_test_email(VERIFIED_USER)


def test_a_test_email_blocked_by_the_daily_cap_is_reported_not_called_queued() -> None:
    env = build_env(
        Settings(
            _env_file=None,
            email_from_address="avisos@finance.example.com",
            email_daily_cap_per_user=1,
        )
    )
    assert env.service.send_test_email(VERIFIED_USER).status == "sent"

    with fails_with("email_delivery_failed", 503):
        env.service.send_test_email(VERIFIED_USER)


# --- unsubscribe ---------------------------------------------------------------------------


def test_unsubscribing_turns_email_off_and_can_be_repeated(env: Env) -> None:
    env.service.update_preferences(
        VERIFIED_USER, email_enabled=True, budget_alerts=None, show_amounts=None
    )

    assert env.service.unsubscribe(VERIFIED_USER) is True
    assert env.service.unsubscribe(VERIFIED_USER) is True

    view = env.service.get_preferences(VERIFIED_USER)
    assert view.email_enabled is False and view.email_verified is True  # still verified
    assert view.budget_alerts is True  # only the master switch changes


def test_unsubscribing_works_even_for_someone_who_never_opted_in(env: Env) -> None:
    assert env.service.unsubscribe(LUIS) is True
    assert env.service.get_preferences(LUIS).email_enabled is False


def test_unsubscribing_an_account_that_no_longer_exists_is_a_quiet_no(env: Env) -> None:
    assert env.service.unsubscribe(999) is False


# --- privacy -------------------------------------------------------------------------------


def test_logs_never_contain_the_address_or_the_code(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env.service.request_verification(ANA)
    code = env.sender.last_code()
    with pytest.raises(AppError):
        env.service.confirm_verification(ANA, "000000" if code != "000000" else "111111")
    env.service.confirm_verification(ANA, code)
    env.service.send_test_email(ANA)
    env.service.unsubscribe(ANA)

    logged = " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert ANA_ADDRESS not in logged and "ana.garcia" not in logged
    assert code not in logged

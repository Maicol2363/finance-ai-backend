"""PostgreSQL proof of the verification flow under real concurrency (EML-02): the single open
code, exact attempt counting under row locks, and first-use preference creation."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from finance_ai.core.config import Settings, get_settings
from finance_ai.core.errors import AppError
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.notifications.account import NotificationAccountService
from finance_ai.notifications.dispatcher import NotificationDispatcher
from finance_ai.notifications.messages import EmailMessage, SendResult
from tests.notification_support import CODE_PATTERN

pytestmark = pytest.mark.integration
TEST_DATABASE_URL = os.getenv("FINANCE_AI_TEST_DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("FINANCE_AI_TEST_DATABASE_URL is not configured")
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        pytest.fail("Integration database name must end with '_test'")
    os.environ["FINANCE_AI_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["FINANCE_AI_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    clear_session_caches()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_command.downgrade(config, "base")
    alembic_command.upgrade(config, "head")
    with get_session_factory()() as session:
        seed_shared_data(session)


def reset_rows() -> None:
    with get_session_factory()() as session:
        session.execute(
            text(
                "TRUNCATE TABLE notification_outbox, notification_preference, "
                "email_verification, app_user RESTART IDENTITY CASCADE"
            )
        )
        session.commit()


@pytest.fixture(autouse=True)
def clean_rows(migrated_database: None) -> Iterator[None]:
    reset_rows()
    yield
    reset_rows()


def make_user(email: str, *, verified: bool = False) -> int:
    with get_session_factory()() as session:
        user_id = session.execute(
            text(
                "INSERT INTO app_user (email, password_hash, role_id, preferred_currency_id, "
                "email_verified_at) VALUES (:email, 'hash', (SELECT id FROM role WHERE "
                "name = 'user'), (SELECT id FROM currency WHERE code = 'COP'), "
                "CASE WHEN :verified THEN now() END) RETURNING id"
            ),
            {"email": email, "verified": verified},
        ).scalar_one()
        session.commit()
        return int(user_id)


class LockedSender:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self._lock = threading.Lock()

    def send(self, message: EmailMessage) -> SendResult:
        with self._lock:
            self.sent.append(message)
        return SendResult(provider_message_id=f"id-{len(self.sent)}@finance.example.com")

    def codes(self) -> list[str]:
        found = []
        for message in self.sent:
            match = CODE_PATTERN.search(message.text_body)
            assert match is not None
            found.append(match.group(1) + match.group(2))
        return found


def build_service(sender: LockedSender) -> NotificationAccountService:
    settings = Settings(_env_file=None, email_from_address="avisos@finance.example.com")
    return NotificationAccountService(
        session_factory=get_session_factory(),
        sender_factory=lambda: sender,
        dispatcher_factory=lambda: NotificationDispatcher(
            session_factory=get_session_factory(),
            sender=sender,
            settings=settings,
            clock=lambda: datetime.now(UTC),
        ),
        settings=settings,
    )


def race(count: int, action: Callable[[int], Any]) -> list[Any]:
    """Run ``action`` in ``count`` threads released together; collect each result or error."""
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def run(index: int) -> None:
        barrier.wait(timeout=10)
        try:
            results[index] = action(index)
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


def scalar(sql: str, **params: object) -> Any:
    with get_session_factory()() as session:
        return session.execute(text(sql), params).scalar_one()


# --- requesting codes ----------------------------------------------------------------------


def test_simultaneous_requests_leave_exactly_one_open_code_and_no_server_error() -> None:
    user = make_user("ana@example.com")
    sender = LockedSender()
    service = build_service(sender)

    results = race(8, lambda _i: service.request_verification(user))

    errors = [r for r in results if isinstance(r, Exception)]
    assert all(isinstance(e, AppError) and e.status == 429 for e in errors), errors
    assert len(results) - len(errors) >= 1  # somebody won
    assert (
        scalar(
            "SELECT count(*) FROM email_verification WHERE consumed_at IS NULL AND user_id = :u",
            u=user,
        )
        == 1
    )
    # Every code that was emailed is a code somebody was told about, and only one is live.
    assert len(sender.sent) == len(results) - len(errors)


# --- confirming codes ----------------------------------------------------------------------


def test_simultaneous_wrong_guesses_are_counted_exactly_and_stop_at_the_limit() -> None:
    user = make_user("ana@example.com")
    sender = LockedSender()
    service = build_service(sender)
    service.request_verification(user)
    real = sender.codes()[0]
    wrong = "000000" if real != "000000" else "111111"

    results = race(12, lambda _i: service.confirm_verification(user, wrong))

    codes = sorted(r.code for r in results if isinstance(r, AppError))
    assert len(codes) == 12  # nobody verified, nobody crashed
    assert codes.count("verification_attempts_exceeded") == 1  # the guess that burned the code
    assert codes.count("verification_code_invalid") == 11
    with pytest.raises(AppError) as late:  # the right code arrives after the burn: too late
        service.confirm_verification(user, real)
    assert late.value.code == "verification_code_invalid"
    assert scalar("SELECT email_verified_at IS NULL FROM app_user WHERE id = :u", u=user) is True


def test_two_simultaneous_correct_codes_verify_once() -> None:
    user = make_user("ana@example.com")
    sender = LockedSender()
    service = build_service(sender)
    service.request_verification(user)
    real = sender.codes()[0]

    results = race(6, lambda _i: service.confirm_verification(user, real))

    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1 and winners[0].email_verified is True
    assert all(
        isinstance(r, AppError) and r.code == "verification_code_invalid"
        for r in results
        if isinstance(r, Exception)
    )


# --- preferences ---------------------------------------------------------------------------


def test_first_use_by_several_requests_at_once_creates_one_row() -> None:
    user = make_user("ana@example.com", verified=True)
    service = build_service(LockedSender())

    results = race(
        8,
        lambda i: service.update_preferences(
            user, email_enabled=True, budget_alerts=None, show_amounts=(i % 2 == 0)
        ),
    )

    assert not [r for r in results if isinstance(r, Exception)], results
    assert scalar("SELECT count(*) FROM notification_preference WHERE user_id = :u", u=user) == 1
    assert scalar("SELECT email_enabled FROM notification_preference WHERE user_id = :u", u=user)


def test_unsubscribing_while_enabling_never_errors_and_ends_consistent() -> None:
    user = make_user("ana@example.com", verified=True)
    service = build_service(LockedSender())

    def action(index: int) -> object:
        if index % 2:
            return service.unsubscribe(user)
        return service.update_preferences(
            user, email_enabled=True, budget_alerts=None, show_amounts=None
        )

    results = race(10, action)

    assert not [r for r in results if isinstance(r, Exception)], results
    assert scalar("SELECT count(*) FROM notification_preference WHERE user_id = :u", u=user) == 1


def test_the_test_email_only_dispatches_the_requesting_persons_rows() -> None:
    marta = make_user("marta@example.com", verified=True)
    other = make_user("otra@example.com", verified=True)
    sender = LockedSender()
    service = build_service(sender)
    with get_session_factory()() as session:
        session.execute(
            text(
                "INSERT INTO notification_outbox (user_id, kind, dedupe_key, payload) "
                "VALUES (:u, 'test', 'theirs', '{}')"
            ),
            {"u": other},
        )
        session.commit()

    assert service.send_test_email(marta).status == "sent"

    assert [m.to_address for m in sender.sent] == ["marta@example.com"]
    assert scalar("SELECT status FROM notification_outbox WHERE dedupe_key = 'theirs'") == "pending"

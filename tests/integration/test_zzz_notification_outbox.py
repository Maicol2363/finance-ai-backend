"""PostgreSQL proof of the notification outbox: idempotent decisions, exclusive claiming and the
constraints that keep the table consistent (EML-01)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from finance_ai.core.config import Settings, get_settings
from finance_ai.db.seeds import seed_shared_data
from finance_ai.db.session import clear_session_caches, get_session_factory
from finance_ai.notifications.dispatcher import NotificationDispatcher
from finance_ai.notifications.messages import EmailMessage, SendResult
from finance_ai.notifications.outbox import enqueue

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


def make_user(email: str, *, verified: bool = True, enabled: bool = True) -> int:
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
        session.execute(
            text("INSERT INTO notification_preference (user_id, email_enabled) VALUES (:u, :e)"),
            {"u": user_id, "e": enabled},
        )
        session.commit()
        return int(user_id)


def payload(name: str) -> dict[str, object]:
    return {
        "budget_name": name,
        "period_start": "2026-11-01",
        "period_end": "2026-11-30",
        "threshold": 80,
        "spent": "480000",
        "limit": "600000",
        "currency_code": "COP",
    }


class SlowRecordingSender:
    def __init__(self) -> None:
        self.subjects: list[str] = []
        self._lock = threading.Lock()

    def send(self, message: EmailMessage) -> SendResult:
        time.sleep(0.01)
        with self._lock:
            self.subjects.append(message.subject)
        return SendResult(provider_message_id=f"id-{len(self.subjects)}@finance.example.com")


def dispatcher(sender: object) -> NotificationDispatcher:
    return NotificationDispatcher(
        session_factory=get_session_factory(),
        sender=sender,  # type: ignore[arg-type]
        settings=Settings(
            _env_file=None,
            email_from_address="avisos@finance.example.com",
            email_daily_cap_per_user=100,
        ),
        clock=lambda: datetime.now(UTC),
    )


# --- idempotent decisions -----------------------------------------------------------------


def test_simultaneous_identical_decisions_write_exactly_one_row() -> None:
    user = make_user("ana@example.com")
    barrier = threading.Barrier(8)
    results: list[bool] = []
    guard = threading.Lock()

    def decide() -> None:
        with get_session_factory()() as session:
            barrier.wait(timeout=10)
            inserted = enqueue(
                session,
                user_id=user,
                kind="budget_alert",
                dedupe_key="budget:1:2026-11-01:80:600000",
                payload={},
            )
            session.commit()
        with guard:
            results.append(inserted)

    threads = [threading.Thread(target=decide) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == [False] * 7 + [True]
    with get_session_factory()() as session:
        assert session.execute(text("SELECT count(*) FROM notification_outbox")).scalar_one() == 1


def test_a_decision_rolled_back_with_its_event_leaves_nothing_behind() -> None:
    user = make_user("ana@example.com")
    with get_session_factory()() as session:
        enqueue(session, user_id=user, kind="budget_alert", dedupe_key="k", payload={})
        session.rollback()

    with get_session_factory()() as session:
        assert session.execute(text("SELECT count(*) FROM notification_outbox")).scalar_one() == 0


# --- exclusive claiming -------------------------------------------------------------------


def test_two_dispatchers_working_together_never_send_the_same_row() -> None:
    user = make_user("ana@example.com")
    with get_session_factory()() as session:
        for number in range(30):
            enqueue(
                session,
                user_id=user,
                kind="budget_alert",
                dedupe_key=f"budget:{number}",
                payload=payload(f"Presupuesto {number:02d}"),
            )
        session.commit()
    sender = SlowRecordingSender()
    summaries: list[int] = []
    guard = threading.Lock()

    def work() -> None:
        sent = dispatcher(sender).dispatch_once(limit=100).sent
        with guard:
            summaries.append(sent)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sum(summaries) == 30 and len(sender.subjects) == 30
    assert len(set(sender.subjects)) == 30  # every budget was announced exactly once
    with get_session_factory()() as session:
        rows = session.execute(text("SELECT status, attempts FROM notification_outbox")).all()
    assert {(row.status, row.attempts) for row in rows} == {("sent", 1)}


def test_a_row_locked_by_another_worker_is_skipped_not_waited_for() -> None:
    user = make_user("ana@example.com")
    with get_session_factory()() as session:
        enqueue(
            session, user_id=user, kind="budget_alert", dedupe_key="k", payload=payload("Mercado")
        )
        session.commit()
    holder = get_session_factory()()
    holder.execute(text("SELECT id FROM notification_outbox FOR UPDATE"))  # worker A holds the row
    try:
        started = time.monotonic()
        summary = dispatcher(SlowRecordingSender()).dispatch_once()
        waited = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    assert summary.sent == 0 and waited < 2
    assert dispatcher(SlowRecordingSender()).dispatch_once().sent == 1  # free again: now sent


# --- constraints --------------------------------------------------------------------------


_ROW = (
    "INSERT INTO notification_outbox (user_id, kind, dedupe_key, payload{cols}) VALUES (:u, {vals})"
)


@pytest.mark.parametrize(
    "statement",
    [
        _ROW.format(cols="", vals="'newsletter', 'a', '{}'"),
        _ROW.format(cols=", status", vals="'test', 'a', '{}', 'weird'"),
        _ROW.format(cols=", status", vals="'test', 'a', '{}', 'sent'"),
        _ROW.format(cols=", status, sent_at", vals="'test', 'a', '{}', 'pending', now()"),
        _ROW.format(cols=", attempts", vals="'test', 'a', '{}', -1"),
    ],
)
def test_the_database_refuses_inconsistent_outbox_rows(statement: str) -> None:
    user = make_user("ana@example.com")
    with get_session_factory()() as session:
        with pytest.raises(IntegrityError):
            session.execute(text(statement), {"u": user})
        session.rollback()


def test_the_dedupe_key_is_unique_at_the_database_level() -> None:
    user = make_user("ana@example.com")
    insert = text(
        "INSERT INTO notification_outbox (user_id, kind, dedupe_key, payload) "
        "VALUES (:u, 'test', 'same', '{}')"
    )
    with get_session_factory()() as session:
        session.execute(insert, {"u": user})
        with pytest.raises(IntegrityError):
            session.execute(insert, {"u": user})
        session.rollback()


def test_only_one_verification_code_can_be_open_per_user() -> None:
    user = make_user("ana@example.com")
    insert = text(
        "INSERT INTO email_verification (user_id, code_hash, expires_at) "
        "VALUES (:u, repeat('a', 64), now() + interval '15 minutes')"
    )
    with get_session_factory()() as session:
        session.execute(insert, {"u": user})
        with pytest.raises(IntegrityError):
            session.execute(insert, {"u": user})
        session.rollback()
    with get_session_factory()() as session:
        session.execute(insert, {"u": user})
        session.execute(
            text("UPDATE email_verification SET consumed_at = now() + interval '1 second'")
        )
        session.execute(insert, {"u": user})  # a new code after the first was used
        session.commit()


def test_deleting_an_account_removes_its_notification_data() -> None:
    user = make_user("ana@example.com")
    with get_session_factory()() as session:
        enqueue(session, user_id=user, kind="test", dedupe_key="k", payload={})
        session.commit()
        session.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": user})
        session.commit()
        for table in ("notification_outbox", "notification_preference"):
            assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0

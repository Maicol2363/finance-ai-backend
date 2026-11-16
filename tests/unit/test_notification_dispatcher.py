from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.core.config import Settings
from finance_ai.db.base import Base
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.notifications import NotificationOutbox, NotificationPreference
from finance_ai.db.models.reference import Currency
from finance_ai.notifications.dispatcher import NotificationDispatcher
from finance_ai.notifications.messages import EmailMessage, EmailSendError, SendResult
from finance_ai.notifications.outbox import enqueue

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
VERIFIED, UNVERIFIED, INACTIVE, OPTED_OUT, NO_ALERTS, HIDE_AMOUNTS = 1, 2, 3, 4, 5, 6
PAYLOAD: dict[str, Any] = {
    "budget_id": 7,
    "budget_name": "Mercado",
    "period_start": "2026-11-01",
    "period_end": "2026-11-30",
    "threshold": 80,
    "spent": "480000.00",
    "limit": "600000.00",
    "currency_code": "COP",
}


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


@dataclass
class RecordingSender:
    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(provider_message_id=f"id-{len(self.sent)}@finance.example.com")


class FailingSender:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def send(self, message: EmailMessage) -> SendResult:
        self.calls += 1
        raise self.error


@dataclass
class Env:
    factory: sessionmaker[Session]
    clock: Clock
    sender: RecordingSender
    dispatcher: NotificationDispatcher
    settings: Settings


def make_dispatcher(
    factory: sessionmaker[Session],
    clock: Clock,
    sender: Any,
    settings: Settings,
    unsubscribe: Any = lambda _user_id: None,
) -> NotificationDispatcher:
    return NotificationDispatcher(
        session_factory=factory,
        sender=sender,
        settings=settings,
        clock=clock,
        unsubscribe_url=unsubscribe,
    )


@pytest.fixture
def env() -> Env:
    engine = create_engine("sqlite://")
    event.listen(
        engine,
        "connect",
        lambda connection, _: (
            connection.create_function(
                "btrim", 1, lambda value: value.strip() if value is not None else None
            ),
            connection.create_function("char_length", 1, len),
        ),
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Role.__table__,
            Currency.__table__,
            AppUser.__table__,
            NotificationPreference.__table__,
            NotificationOutbox.__table__,
        ],  # type: ignore[arg-type]
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    verified_at = NOW - timedelta(days=1)
    with factory() as session, session.begin():
        session.add_all(
            [Role(id=1, name="user"), Currency(id=1, code="COP", name="Peso", decimals=2)]
        )
        session.flush()
        for user_id, email, active, verified in (
            (VERIFIED, "ana@example.com", True, True),
            (UNVERIFIED, "luis@example.com", True, False),
            (INACTIVE, "inactiva@example.com", False, True),
            (OPTED_OUT, "no-quiere@example.com", True, True),
            (NO_ALERTS, "sin-alertas@example.com", True, True),
            (HIDE_AMOUNTS, "privado@example.com", True, True),
        ):
            session.add(
                AppUser(
                    id=user_id,
                    email=email,
                    password_hash="hash",
                    role_id=1,
                    preferred_currency_id=1,
                    is_active=active,
                    email_verified_at=verified_at if verified else None,
                )
            )
        session.flush()
        for user_id, enabled, alerts, amounts in (
            (VERIFIED, True, True, True),
            (UNVERIFIED, True, True, True),
            (INACTIVE, True, True, True),
            (OPTED_OUT, False, True, True),
            (NO_ALERTS, True, False, True),
            (HIDE_AMOUNTS, True, True, False),
        ):
            session.add(
                NotificationPreference(
                    user_id=user_id,
                    email_enabled=enabled,
                    budget_alerts=alerts,
                    show_amounts=amounts,
                )
            )
    clock, sender = Clock(), RecordingSender()
    settings = Settings(_env_file=None, email_from_address="avisos@finance.example.com")
    return Env(factory, clock, sender, make_dispatcher(factory, clock, sender, settings), settings)


def queue(
    env: Env, user_id: int = VERIFIED, *, kind: str = "budget_alert", key: str | None = None
) -> int:
    with env.factory() as session, session.begin():
        enqueue(
            session,
            user_id=user_id,
            kind=kind,
            dedupe_key=key
            or f"{kind}:{user_id}:{len(list(session.scalars(select(NotificationOutbox))))}",
            payload=dict(PAYLOAD) if kind == "budget_alert" else {},
            not_before=NOW,
        )
        return int(
            session.scalars(
                select(NotificationOutbox.id).order_by(NotificationOutbox.id.desc())
            ).first()
            or 0
        )


def row(env: Env, row_id: int) -> NotificationOutbox:
    with env.factory() as session:
        return session.get(NotificationOutbox, row_id)  # type: ignore[return-value]


# --- outbox -------------------------------------------------------------------------------


def test_the_same_decision_is_recorded_only_once() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[NotificationOutbox.__table__])  # type: ignore[list-item]
    factory = sessionmaker(bind=engine)
    with factory() as session, session.begin():
        assert enqueue(session, user_id=1, kind="budget_alert", dedupe_key="k", payload={}) is True
        assert enqueue(session, user_id=1, kind="budget_alert", dedupe_key="k", payload={}) is False
        assert len(list(session.scalars(select(NotificationOutbox)))) == 1
        with pytest.raises(ValueError):
            enqueue(session, user_id=1, kind="newsletter", dedupe_key="x", payload={})


# --- sending ------------------------------------------------------------------------------


def test_a_budget_alert_is_rendered_sent_and_recorded(env: Env) -> None:
    row_id = queue(env)

    summary = env.dispatcher.dispatch_once()

    assert (summary.sent, summary.skipped, summary.retried, summary.failed) == (1, 0, 0, 0)
    message = env.sender.sent[0]
    assert message.to_address == "ana@example.com"
    assert (
        message.from_address == "avisos@finance.example.com" and message.from_name == "Finance AI"
    )
    assert message.subject == "Llevas el 80 % de tu presupuesto «Mercado»"
    assert "$ 480.000" in message.html_body and "Gastado: $ 480.000" in message.text_body
    assert len(message.inline_images) == 1 and "List-Unsubscribe" not in message.headers
    stored = row(env, row_id)
    assert stored.status == "sent" and stored.attempts == 1 and stored.sent_at is not None
    assert (
        stored.provider_message_id == "id-1@finance.example.com" and stored.last_error_code is None
    )
    assert env.dispatcher.dispatch_once().sent == 0  # nothing is sent twice


def test_the_unsubscribe_link_is_added_as_a_visible_link_and_as_headers(env: Env) -> None:
    link = "https://api.example.com/public/unsubscribe?t=abc"
    dispatcher = make_dispatcher(env.factory, env.clock, env.sender, env.settings, lambda _u: link)
    queue(env)

    dispatcher.dispatch_once()

    message = env.sender.sent[0]
    assert message.headers["List-Unsubscribe"] == f"<{link}>"
    assert message.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert link in message.html_body and link in message.text_body


def test_hiding_amounts_removes_them_from_the_email(env: Env) -> None:
    queue(env, HIDE_AMOUNTS)

    env.dispatcher.dispatch_once()

    message = env.sender.sent[0]
    assert "480.000" not in message.html_body and "480.000" not in message.text_body
    assert "80 %" in message.html_body


@pytest.mark.parametrize(
    ("user_id", "kind", "reason"),
    [
        (UNVERIFIED, "budget_alert", "email_not_verified"),
        (INACTIVE, "budget_alert", "user_inactive"),
        (OPTED_OUT, "budget_alert", "notifications_disabled"),
        (NO_ALERTS, "budget_alert", "budget_alerts_disabled"),
        (999, "budget_alert", "user_missing"),
        (UNVERIFIED, "test", "email_not_verified"),
        (INACTIVE, "test", "user_inactive"),
    ],
)
def test_ineligible_recipients_are_skipped_at_send_time(
    env: Env, user_id: int, kind: str, reason: str
) -> None:
    row_id = queue(env, user_id, kind=kind)

    summary = env.dispatcher.dispatch_once()

    assert summary.skipped == 1 and summary.sent == 0 and env.sender.sent == []
    stored = row(env, row_id)
    assert stored.status == "skipped" and stored.last_error_code == reason


def test_the_test_email_goes_to_a_verified_address_even_when_alerts_are_off(env: Env) -> None:
    queue(env, OPTED_OUT, kind="test")

    env.dispatcher.dispatch_once()

    assert env.sender.sent[0].subject == "Correo de prueba de Finance AI"


def test_the_daily_cap_counts_only_the_last_24_hours(env: Env) -> None:
    settings = env.settings.model_copy(update={"email_daily_cap_per_user": 2})
    dispatcher = make_dispatcher(env.factory, env.clock, env.sender, settings)
    with env.factory() as session, session.begin():
        for index, age in enumerate((timedelta(hours=1), timedelta(hours=5), timedelta(hours=30))):
            session.add(
                NotificationOutbox(
                    user_id=VERIFIED,
                    kind="test",
                    dedupe_key=f"old-{index}",
                    payload={},
                    status="sent",
                    sent_at=NOW - age,
                )
            )
    capped = queue(env, key="new-1")

    dispatcher.dispatch_once()

    assert row(env, capped).status == "skipped" and row(env, capped).last_error_code == "daily_cap"
    assert env.sender.sent == []


# --- failures -----------------------------------------------------------------------------


def test_a_temporary_failure_is_retried_with_growing_delays_then_given_up(env: Env) -> None:
    sender = FailingSender(EmailSendError("provider_unavailable", retryable=True))
    dispatcher = make_dispatcher(env.factory, env.clock, sender, env.settings)
    row_id = queue(env)

    delays: list[float] = []
    for _ in range(env.settings.email_max_attempts):
        before = env.clock.value
        summary = dispatcher.dispatch_once()
        stored = row(env, row_id)
        if stored.status == "pending":
            assert summary.retried == 1
            delays.append(
                (stored.next_attempt_at.replace(tzinfo=UTC) - before).total_seconds() / 60
            )
            assert stored.last_error_code == "provider_unavailable"
            # Not due yet: nothing happens until the delay has passed.
            assert dispatcher.dispatch_once().retried == 0
            env.clock.value = stored.next_attempt_at.replace(tzinfo=UTC)

    stored = row(env, row_id)
    assert delays == [1, 5, 15, 60]
    assert stored.status == "failed" and stored.attempts == 5
    assert sender.calls == 5 and stored.last_error_code == "provider_unavailable"


def test_a_final_failure_is_not_retried(env: Env) -> None:
    sender = FailingSender(EmailSendError("recipient_rejected", retryable=False))
    dispatcher = make_dispatcher(env.factory, env.clock, sender, env.settings)
    row_id = queue(env)

    summary = dispatcher.dispatch_once()

    assert summary.failed == 1 and row(env, row_id).status == "failed"
    assert dispatcher.dispatch_once().failed == 0 and sender.calls == 1


def test_an_unexpected_error_is_retried_without_keeping_or_logging_its_text(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    sender = FailingSender(RuntimeError("could not deliver to ana@example.com: secret detail"))
    dispatcher = make_dispatcher(env.factory, env.clock, sender, env.settings)
    row_id = queue(env)

    with caplog.at_level(logging.DEBUG):
        summary = dispatcher.dispatch_once()

    stored = row(env, row_id)
    assert summary.retried == 1 and stored.last_error_code == "send_failed"
    for text in (caplog.text, str(stored.last_error_code), str(stored.payload)):
        assert "ana@example.com" not in text and "secret detail" not in text


# --- claiming -----------------------------------------------------------------------------


def test_a_row_stuck_in_sending_is_recovered_after_the_timeout(env: Env) -> None:
    stuck, recent = queue(env, key="stuck"), queue(env, key="recent")
    with env.factory() as session, session.begin():
        for row_id, age in ((stuck, 11), (recent, 5)):
            item = session.get(NotificationOutbox, row_id)
            assert item is not None
            item.status, item.claimed_at = "sending", NOW - timedelta(minutes=age)

    env.dispatcher.dispatch_once()

    assert row(env, stuck).status == "sent"
    assert row(env, recent).status == "sending"  # another instance may still be working on it


def test_the_limit_bounds_one_run_and_rows_go_in_order(env: Env) -> None:
    ids = [queue(env, key=f"k{n}") for n in range(4)]

    summary = env.dispatcher.dispatch_once(limit=2)

    assert summary.sent == 2
    assert [row(env, i).status for i in ids] == ["sent", "sent", "pending", "pending"]


def test_rows_scheduled_for_later_wait_for_their_time(env: Env) -> None:
    row_id = queue(env)
    with env.factory() as session, session.begin():
        item = session.get(NotificationOutbox, row_id)
        assert item is not None
        item.next_attempt_at = NOW + timedelta(minutes=30)

    assert env.dispatcher.dispatch_once().sent == 0
    env.clock.value = NOW + timedelta(minutes=31)
    assert env.dispatcher.dispatch_once().sent == 1

"""Shared fixtures for the notification account tests: an in-memory database with three people
(two unverified, one verified), a controllable clock and a recording sender."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from finance_ai.core.config import Settings
from finance_ai.db.base import Base
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.notifications import (
    EmailVerification,
    NotificationOutbox,
    NotificationPreference,
)
from finance_ai.db.models.reference import Currency
from finance_ai.notifications.account import NotificationAccountService
from finance_ai.notifications.dispatcher import NotificationDispatcher
from finance_ai.notifications.messages import EmailMessage, EmailSendError, SendResult

ANA, LUIS, VERIFIED_USER = 1, 2, 3
ANA_ADDRESS = "ana.garcia@example.com"
CODE_PATTERN = re.compile(r"\b(\d{3}) (\d{3})\b")


class Clock:
    def __init__(self) -> None:
        self.value = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta: int) -> None:
        self.value += timedelta(**delta)


@dataclass
class RecordingSender:
    sent: list[EmailMessage] = field(default_factory=list)
    failure: EmailSendError | None = None

    def send(self, message: EmailMessage) -> SendResult:
        if self.failure is not None:
            raise self.failure
        self.sent.append(message)
        return SendResult(provider_message_id=f"id-{len(self.sent)}@finance.example.com")

    def last_code(self) -> str:
        match = CODE_PATTERN.search(self.sent[-1].text_body)
        assert match is not None
        return match.group(1) + match.group(2)


@dataclass
class Env:
    factory: sessionmaker[Session]
    clock: Clock
    sender: RecordingSender
    service: NotificationAccountService
    settings: Settings


def build_env(settings: Settings | None = None) -> Env:
    settings = settings or Settings(_env_file=None, email_from_address="avisos@finance.example.com")
    # One shared connection: the HTTP test client runs requests on another thread, and every
    # new in-memory SQLite connection would otherwise be an empty database.
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
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
            EmailVerification.__table__,
            NotificationOutbox.__table__,
        ],  # type: ignore[arg-type]
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    clock = Clock()
    with factory() as session, session.begin():
        session.add_all(
            [Role(id=1, name="user"), Currency(id=1, code="COP", name="Peso", decimals=2)]
        )
        session.flush()
        for user_id, email, verified in (
            (ANA, ANA_ADDRESS, False),
            (LUIS, "luis.perez@example.com", False),
            (VERIFIED_USER, "marta@example.com", True),
        ):
            session.add(
                AppUser(
                    id=user_id,
                    email=email,
                    password_hash="hash",
                    role_id=1,
                    preferred_currency_id=1,
                    email_verified_at=clock() - timedelta(days=1) if verified else None,
                )
            )
    sender = RecordingSender()
    service = NotificationAccountService(
        session_factory=factory,
        sender_factory=lambda: sender,
        dispatcher_factory=lambda: NotificationDispatcher(
            session_factory=factory, sender=sender, settings=settings, clock=clock
        ),
        settings=settings,
        clock=clock,
    )
    return Env(factory=factory, clock=clock, sender=sender, service=service, settings=settings)

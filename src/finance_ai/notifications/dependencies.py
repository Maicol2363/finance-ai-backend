"""Wiring of the notification services shared by the endpoints and the scripts."""

from __future__ import annotations

from finance_ai.core.config import Settings, get_settings
from finance_ai.db.session import get_session_factory
from finance_ai.notifications.account import NotificationAccountService
from finance_ai.notifications.dispatcher import NotificationDispatcher
from finance_ai.notifications.senders import create_email_sender
from finance_ai.notifications.tokens import unsubscribe_url_factory


def build_dispatcher(settings: Settings | None = None) -> NotificationDispatcher:
    settings = settings or get_settings()
    return NotificationDispatcher(
        session_factory=get_session_factory(),
        sender=create_email_sender(settings),
        settings=settings,
        unsubscribe_url=unsubscribe_url_factory(settings),
    )


def get_notification_account_service() -> NotificationAccountService:
    settings = get_settings()
    return NotificationAccountService(
        session_factory=get_session_factory(),
        sender_factory=lambda: create_email_sender(settings),
        dispatcher_factory=lambda: build_dispatcher(settings),
        settings=settings,
    )

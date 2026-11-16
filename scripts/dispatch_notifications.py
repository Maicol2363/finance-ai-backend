"""Send what is waiting in the notification outbox, once (local development and operations).

    uv run python scripts/dispatch_notifications.py

The deployed service does the same through the scheduled ``/internal/notifications/dispatch``
endpoint. Uses the configured sender and database.
"""

from __future__ import annotations

from finance_ai.core.config import get_settings
from finance_ai.db.session import get_session_factory
from finance_ai.notifications.dispatcher import NotificationDispatcher
from finance_ai.notifications.senders import create_email_sender


def main() -> None:
    settings = get_settings()
    dispatcher = NotificationDispatcher(
        session_factory=get_session_factory(),
        sender=create_email_sender(settings),
        settings=settings,
    )
    summary = dispatcher.dispatch_once()
    print(
        f"sent={summary.sent} skipped={summary.skipped} "
        f"retried={summary.retried} failed={summary.failed}"
    )


if __name__ == "__main__":
    main()

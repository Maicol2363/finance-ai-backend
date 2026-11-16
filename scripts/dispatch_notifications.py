"""Send what is waiting in the notification outbox, once (local development and operations).

    uv run python scripts/dispatch_notifications.py

The deployed service does the same through the scheduled ``/internal/notifications/dispatch``
endpoint. Uses the configured sender and database.
"""

from __future__ import annotations

from finance_ai.notifications.dependencies import build_dispatcher


def main() -> None:
    summary = build_dispatcher().dispatch_once()
    print(
        f"sent={summary.sent} skipped={summary.skipped} "
        f"retried={summary.retried} failed={summary.failed}"
    )


if __name__ == "__main__":
    main()

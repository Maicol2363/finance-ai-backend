"""Write every email template with sample data to a folder, for review and evidence.

    uv run python scripts/preview_emails.py var/email-preview

Each message becomes ``<id>.eml`` (the raw message), ``<id>.html`` (open it in a browser; the
logo is saved next to it) and the logo image. Nothing is delivered and no database is used.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_ai.notifications.messages import EmailMessage
from finance_ai.notifications.rendering import (
    RenderedEmail,
    render_budget_alert,
    render_test_email,
    render_verification_code,
)
from finance_ai.notifications.senders import ConsoleEmailSender

SAMPLE_UNSUBSCRIBE = "https://api.example.com/public/unsubscribe?t=sample"


def samples() -> dict[str, RenderedEmail]:
    common = {
        "budget_name": "Mercado",
        "period_start": date(2026, 11, 1),
        "period_end": date(2026, 11, 30),
        "currency_code": "COP",
        "limit": Decimal("600000"),
        "show_amounts": True,
        "unsubscribe_url": SAMPLE_UNSUBSCRIBE,
    }
    return {
        "budget-80": render_budget_alert(threshold=80, spent=Decimal("480000"), **common),  # type: ignore[arg-type]
        "budget-100": render_budget_alert(threshold=100, spent=Decimal("602500"), **common),  # type: ignore[arg-type]
        "verification": render_verification_code(code="482913", minutes=15),
        "test": render_test_email(unsubscribe_url=SAMPLE_UNSUBSCRIBE),
    }


def main() -> None:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "var/email-preview")
    sender = ConsoleEmailSender(directory)
    for name, email in samples().items():
        sender.send(
            EmailMessage(
                to_address="persona@example.com",
                subject=email.subject,
                html_body=email.html,
                text_body=email.text,
                from_address="avisos@example.com",
                from_name="Finance AI",
                inline_images=email.inline_images,
            )
        )
        print(f"{name}: {email.subject}")
    print(f"Written to {directory.resolve()}")


if __name__ == "__main__":
    main()

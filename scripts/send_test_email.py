"""Send one real test email through the configured sender (evidence for the notifications card).

    uv run python scripts/send_test_email.py persona@example.com

It uses the same sender, templates and settings as the application, without a database: with
the default console sender it writes the .eml and a browsable .html under
``FINANCE_AI_EMAIL_CONSOLE_DIRECTORY``; with ``FINANCE_AI_EMAIL_PROVIDER=smtp`` and the
``FINANCE_AI_EMAIL_SMTP_*`` settings it delivers to a sandbox inbox or a real address.
Nothing about the recipient is stored or logged.
"""

from __future__ import annotations

import sys

from finance_ai.core.config import get_settings
from finance_ai.notifications.messages import EmailMessage, EmailSendError
from finance_ai.notifications.rendering import render_test_email
from finance_ai.notifications.senders import create_email_sender


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: send_test_email.py <recipient address>")
        return 2
    settings = get_settings()
    rendered = render_test_email(unsubscribe_url=None)
    message = EmailMessage(
        to_address=sys.argv[1],
        subject=rendered.subject,
        html_body=rendered.html,
        text_body=rendered.text,
        from_address=settings.email_from_address or "no-reply@finance-ai.invalid",
        from_name=settings.email_from_name,
        reply_to=settings.email_reply_to,
        inline_images=rendered.inline_images,
    )
    try:
        result = create_email_sender(settings).send(message)
    except EmailSendError as error:
        print(f"Not sent: {error.code} (retryable: {error.retryable})")
        return 1
    print(
        f"Sent with the {settings.email_provider} sender. Message-ID: {result.provider_message_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

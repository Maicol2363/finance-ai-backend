"""Email senders behind one small interface.

``ConsoleEmailSender`` writes the message to disk for development, previews and tests and never
delivers anything. ``SmtpEmailSender`` speaks SMTP using only the standard library, so it works
with a sandbox inbox and with any provider that offers SMTP. Failures are mapped to stable codes
in ``EmailSendError``; nothing here logs addresses or message content.
"""

from __future__ import annotations

import re
import smtplib
import ssl
import uuid
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Protocol

from email_validator import EmailNotValidError, validate_email

from finance_ai.core.config import Settings
from finance_ai.core.errors import AppError
from finance_ai.notifications.messages import EmailMessage, EmailSendError, SendResult

_HEADER_FORBIDDEN = re.compile(r"[\r\n]")
_MAX_SUBJECT_LENGTH = 200


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> SendResult: ...


def _clean_header(name: str, value: str) -> str:
    if _HEADER_FORBIDDEN.search(value):
        raise EmailSendError(f"invalid_header_{name.lower().replace('-', '_')}", retryable=False)
    return value


def _validated_address(value: str) -> str:
    try:
        return validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError:
        raise EmailSendError("invalid_recipient", retryable=False) from None


def build_mime(message: EmailMessage) -> tuple[MimeMessage, str]:
    """Build the MIME message: text and HTML alternatives, with inline images tied to the HTML.

    Returns the message and its Message-ID (without angle brackets).
    """
    recipient = _validated_address(message.to_address)
    sender = _validated_address(message.from_address)
    subject = _clean_header("Subject", message.subject).strip()
    if not subject or len(subject) > _MAX_SUBJECT_LENGTH:
        raise EmailSendError("invalid_header_subject", retryable=False)
    display_name = _clean_header("From", message.from_name)

    mime = MimeMessage(policy=policy.SMTP)
    mime["Subject"] = subject
    mime["From"] = formataddr((display_name, sender))
    mime["To"] = recipient
    if message.reply_to:
        mime["Reply-To"] = _validated_address(message.reply_to)
    mime["Date"] = formatdate(localtime=False)
    message_id = make_msgid(domain=sender.rpartition("@")[2])
    mime["Message-ID"] = message_id
    mime["Auto-Submitted"] = "auto-generated"
    for name, value in message.headers.items():
        mime[_clean_header("Header-Name", name)] = _clean_header(name, value)

    mime.set_content(message.text_body)
    mime.add_alternative(message.html_body, subtype="html")
    html_part = mime.get_body(preferencelist=("html",))
    assert html_part is not None
    for image in message.inline_images:
        maintype, _, subtype = image.mime_type.partition("/")
        html_part.add_related(
            image.content,
            maintype=maintype,
            subtype=subtype,
            cid=f"<{image.content_id}>",
            filename=image.filename,
            disposition="inline",
        )
    return mime, message_id.strip("<>")


class ConsoleEmailSender:
    """Development sender: writes ``<id>.eml`` and a browsable ``<id>.html`` into a directory.

    The HTML copy points ``cid:`` images at files saved next to it, so it opens in a browser
    exactly as the logo and layout will look. File names never contain the recipient.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def send(self, message: EmailMessage) -> SendResult:
        mime, message_id = build_mime(message)
        self._directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        stem = f"{stamp}-{uuid.uuid4().hex[:8]}"
        (self._directory / f"{stem}.eml").write_bytes(mime.as_bytes())
        html = message.html_body
        for image in message.inline_images:
            image_name = f"{stem}-{image.filename}"
            (self._directory / image_name).write_bytes(image.content)
            html = html.replace(f"cid:{image.content_id}", image_name)
        (self._directory / f"{stem}.html").write_text(html, encoding="utf-8")
        return SendResult(provider_message_id=message_id)


class SmtpEmailSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        security: str,
        timeout_seconds: float,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._security = security
        self._timeout = timeout_seconds

    def send(self, message: EmailMessage) -> SendResult:
        mime, message_id = build_mime(message)
        try:
            with self._connect() as smtp:
                if self._security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if self._username is not None and self._password is not None:
                    smtp.login(self._username, self._password)
                smtp.send_message(mime)
        except smtplib.SMTPAuthenticationError:
            raise EmailSendError("provider_auth_failed", retryable=False) from None
        except smtplib.SMTPRecipientsRefused:
            raise EmailSendError("recipient_rejected", retryable=False) from None
        except smtplib.SMTPSenderRefused:
            raise EmailSendError("sender_rejected", retryable=False) from None
        except smtplib.SMTPResponseException as error:
            # 4xx replies are temporary, 5xx are final.
            raise EmailSendError("provider_error", retryable=400 <= error.smtp_code < 500) from None
        except smtplib.SMTPException, OSError, ssl.SSLError:
            raise EmailSendError("provider_unavailable", retryable=True) from None
        return SendResult(provider_message_id=message_id)

    def _connect(self) -> smtplib.SMTP:
        if self._security == "ssl":
            return smtplib.SMTP_SSL(
                self._host,
                self._port,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        return smtplib.SMTP(self._host, self._port, timeout=self._timeout)


class NotificationsUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            status=503,
            code="notifications_unavailable",
            title="Notifications are not available",
            detail="Email notifications are not configured right now. Try again later.",
        )


def delivery_available(settings: Settings) -> bool:
    """Whether ``create_email_sender`` would hand back a sender (it refuses the console sender in
    production). Lets the app hide the feature instead of failing on use."""
    return settings.email_provider == "smtp" or settings.environment != "production"


def create_email_sender(settings: Settings) -> EmailSender:
    """The configured sender. In production the console sender is refused (fails closed), so a
    deployment without a real provider reports ``notifications_unavailable`` instead of
    pretending to deliver mail."""
    if settings.email_provider == "smtp":
        assert settings.email_smtp_host is not None  # guaranteed by the settings validator
        return SmtpEmailSender(
            host=settings.email_smtp_host,
            port=settings.email_smtp_port,
            username=settings.email_smtp_username,
            password=(
                settings.email_smtp_password.get_secret_value()
                if settings.email_smtp_password is not None
                else None
            ),
            security=settings.email_smtp_security,
            timeout_seconds=settings.email_smtp_timeout_seconds,
        )
    if settings.environment == "production":
        raise NotificationsUnavailableError()
    return ConsoleEmailSender(settings.email_console_directory)

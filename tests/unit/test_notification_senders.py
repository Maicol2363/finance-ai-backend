from __future__ import annotations

import smtplib
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any, ClassVar

import pytest

from finance_ai.core.config import Settings
from finance_ai.notifications.messages import EmailMessage, EmailSendError, InlineImage
from finance_ai.notifications.senders import (
    ConsoleEmailSender,
    NotificationsUnavailableError,
    SmtpEmailSender,
    build_mime,
    create_email_sender,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def message(**overrides: Any) -> EmailMessage:
    values: dict[str, Any] = {
        "to_address": "person@example.com",
        "subject": "Hola",
        "html_body": '<p>Hola <img src="cid:logo"></p>',
        "text_body": "Hola",
        "from_address": "avisos@finance.example.com",
        "from_name": "Finance AI",
        "inline_images": (InlineImage(content_id="logo", content=PNG),),
    }
    values.update(overrides)
    return EmailMessage(**values)


# --- MIME ---------------------------------------------------------------------------------


def test_the_message_has_text_and_html_alternatives_with_the_image_tied_to_the_html() -> None:
    mime, message_id = build_mime(message(reply_to="ayuda@finance.example.com"))

    assert mime.get_content_type() == "multipart/alternative"
    text, html = mime.get_payload()  # type: ignore[misc]
    assert (
        text.get_content_type() == "text/plain" and html.get_content_type() == "multipart/related"
    )
    parts = html.get_payload()
    assert parts[0].get_content_type() == "text/html"
    assert parts[1].get_content_type() == "image/png" and parts[1]["Content-ID"] == "<logo>"
    assert parts[1].get_content_disposition() == "inline"
    assert mime["To"] == "person@example.com" and mime["Reply-To"] == "ayuda@finance.example.com"
    assert mime["From"] == "Finance AI <avisos@finance.example.com>"
    assert mime["Auto-Submitted"] == "auto-generated"
    assert message_id.endswith("@finance.example.com") and mime["Message-ID"] == f"<{message_id}>"


def test_extra_headers_such_as_list_unsubscribe_are_added() -> None:
    mime, _ = build_mime(
        message(headers={"List-Unsubscribe": "<https://x.example/u?t=1>", "X-Kind": "budget"})
    )

    assert mime["List-Unsubscribe"] == "<https://x.example/u?t=1>" and mime["X-Kind"] == "budget"


def test_a_long_unsubscribe_link_stays_a_plain_header_on_the_wire() -> None:
    # A real link is longer than one folded line and has no spaces to fold at. It must not be
    # turned into an RFC 2047 encoded word: mail clients read this header literally.
    url = "https://api.finance.example.com/public/unsubscribe?token=v1.1234." + "A" * 43
    mime, _ = build_mime(
        message(
            headers={
                "List-Unsubscribe": f"<{url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            }
        )
    )

    wire = mime.as_bytes()
    header_block = wire.split(b"\r\n\r\n", 1)[0]
    assert f"List-Unsubscribe: <{url}>".encode() in header_block.replace(b"\r\n ", b" ")
    assert b"=?utf-8?" not in b"".join(
        line for line in header_block.split(b"\r\n") if line.startswith(b"List-Unsubscribe")
    )
    parsed = message_from_bytes(wire, policy=policy.default)
    assert str(parsed["List-Unsubscribe"]) == f"<{url}>"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("subject", "Hola\r\nBcc: x@example.com", "invalid_header_subject"),
        ("subject", "   ", "invalid_header_subject"),
        ("subject", "x" * 201, "invalid_header_subject"),
        ("from_name", "Finance\nAI", "invalid_header_from"),
        ("to_address", "person@example.com\r\nBcc: x@example.com", "invalid_recipient"),
        ("to_address", "not-an-address", "invalid_recipient"),
        ("from_address", "nope", "invalid_recipient"),
    ],
)
def test_header_injection_and_bad_addresses_are_refused_without_a_provider_call(
    field: str, value: str, code: str
) -> None:
    with pytest.raises(EmailSendError) as failure:
        build_mime(message(**{field: value}))

    assert failure.value.code == code and failure.value.retryable is False


def test_a_forged_header_name_or_value_is_refused() -> None:
    with pytest.raises(EmailSendError):
        build_mime(message(headers={"X-Ok\nBcc": "a"}))
    with pytest.raises(EmailSendError):
        build_mime(message(headers={"X-Ok": "a\r\nBcc: x@example.com"}))


# --- console sender -----------------------------------------------------------------------


def test_the_console_sender_writes_an_eml_and_a_browsable_html_without_the_recipient_in_names(
    tmp_path: Path,
) -> None:
    result = ConsoleEmailSender(tmp_path / "preview").send(message())

    files = sorted(path.name for path in (tmp_path / "preview").iterdir())
    assert len(files) == 3 and not any("example.com" in name or "person" in name for name in files)
    eml = next(path for path in (tmp_path / "preview").glob("*.eml"))
    parsed = message_from_bytes(eml.read_bytes(), policy=policy.default)
    assert (
        parsed["To"] == "person@example.com"
        and parsed["Message-ID"] == f"<{result.provider_message_id}>"
    )
    html = next(path for path in (tmp_path / "preview").glob("*.html")).read_text(encoding="utf-8")
    assert "cid:" not in html and "logo.png" in html  # the preview opens with its logo


# --- SMTP sender --------------------------------------------------------------------------


class FakeSmtp:
    instances: ClassVar[list[FakeSmtp]] = []

    def __init__(self, host: str, port: int, timeout: float = 0, **_: Any) -> None:
        self.calls: list[str] = []
        self.sent: list[Any] = []
        self.host, self.port, self.timeout = host, port, timeout
        FakeSmtp.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def starttls(self, **_: Any) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(f"login:{username}")

    def send_message(self, mime: Any) -> None:
        self.calls.append("send")
        self.sent.append(mime)


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSmtp]:
    FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    return FakeSmtp


def smtp(
    security: str = "starttls", user: str | None = "user", pw: str | None = "pw"
) -> SmtpEmailSender:
    return SmtpEmailSender(
        host="smtp.example.com",
        port=587,
        username=user,
        password=pw,
        security=security,
        timeout_seconds=5,
    )


@pytest.mark.parametrize(
    ("security", "calls"),
    [
        ("starttls", ["starttls", "login:user", "send"]),
        ("ssl", ["login:user", "send"]),
        ("none", ["login:user", "send"]),
    ],
)
def test_the_smtp_sender_secures_authenticates_and_sends(
    fake_smtp: type[FakeSmtp], security: str, calls: list[str]
) -> None:
    result = smtp(security).send(message())

    assert fake_smtp.instances[0].calls == calls
    assert fake_smtp.instances[0].sent[0]["Message-ID"] == f"<{result.provider_message_id}>"


def test_a_local_inbox_without_credentials_skips_login(fake_smtp: type[FakeSmtp]) -> None:
    smtp("none", user=None, pw=None).send(message())

    assert fake_smtp.instances[0].calls == ["send"]


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), "provider_auth_failed", False),
        (smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"no")}), "recipient_rejected", False),
        (smtplib.SMTPSenderRefused(553, b"no", "a@b.c"), "sender_rejected", False),
        (smtplib.SMTPResponseException(450, b"try later"), "provider_error", True),
        (smtplib.SMTPResponseException(554, b"rejected"), "provider_error", False),
        (smtplib.SMTPServerDisconnected("gone"), "provider_unavailable", True),
        (TimeoutError("slow"), "provider_unavailable", True),
        (ConnectionRefusedError("down"), "provider_unavailable", True),
    ],
)
def test_provider_failures_become_stable_codes_without_provider_text(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str, retryable: bool
) -> None:
    class Failing(FakeSmtp):
        def send_message(self, mime: Any) -> None:
            raise error

    monkeypatch.setattr(smtplib, "SMTP", Failing)

    with pytest.raises(EmailSendError) as failure:
        smtp().send(message())

    assert failure.value.code == code and failure.value.retryable is retryable
    assert "credentials" not in str(failure.value) and "person@example.com" not in str(
        failure.value
    )


# --- choosing the sender ------------------------------------------------------------------


def test_local_development_defaults_to_the_console_sender(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, environment="local", email_console_directory=tmp_path)

    assert isinstance(create_email_sender(settings), ConsoleEmailSender)


def test_production_without_a_real_provider_fails_closed() -> None:
    settings = Settings(_env_file=None, environment="test")
    production = settings.model_copy(update={"environment": "production"})

    with pytest.raises(NotificationsUnavailableError) as failure:
        create_email_sender(production)

    assert failure.value.status == 503 and failure.value.code == "notifications_unavailable"


def test_an_smtp_configuration_builds_the_smtp_sender() -> None:
    settings = Settings(
        _env_file=None,
        email_provider="smtp",
        email_smtp_host="smtp.example.com",
        email_from_address="avisos@finance.example.com",
    )

    assert isinstance(create_email_sender(settings), SmtpEmailSender)


# --- settings -----------------------------------------------------------------------------


def test_email_settings_have_safe_defaults_and_blank_values_mean_unconfigured() -> None:
    settings = Settings(_env_file=None, email_from_address="  ", notification_dispatch_token="")

    assert settings.email_provider == "console"
    assert settings.email_from_address is None and settings.notification_dispatch_token is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"email_provider": "smtp"},
        {"email_provider": "smtp", "email_smtp_host": "smtp.example.com"},
        {
            "email_provider": "smtp",
            "email_smtp_host": "smtp.example.com",
            "email_from_address": "a@example.com",
            "email_smtp_username": "user",
        },
        {"email_from_address": "Finance AI <a@example.com>"},
        {"email_from_address": "a@example.com\r\nBcc: b@example.com"},
        {"email_reply_to": "not-an-address"},
        {"public_base_url": "ftp://api.example.com"},
        {"public_base_url": "https://api.example.com/"},
        {"notification_dispatch_token": "too-short"},
    ],
)
def test_inconsistent_email_settings_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)


def test_production_requires_encrypted_smtp_and_an_https_base_url() -> None:
    base: dict[str, Any] = {
        "_env_file": None,
        "environment": "production",
        "database_url": "postgresql+psycopg://u:p@db.example.com/finance_ai",
        "session_limiter_secret": "a-production-limiter-secret-value-1",
        "email_provider": "smtp",
        "email_smtp_host": "smtp.example.com",
        "email_from_address": "avisos@finance.example.com",
    }
    Settings(**base, public_base_url="https://api.example.com")
    with pytest.raises(ValueError):
        Settings(**base, email_smtp_security="none")
    with pytest.raises(ValueError):
        Settings(**base, public_base_url="http://api.example.com")


# --- a real SMTP conversation over a socket -----------------------------------------------


class _SmtpSink:
    """A minimal SMTP server that records the envelope and the DATA it receives."""

    def __init__(self) -> None:
        import socketserver

        sink = self
        self.messages: list[bytes] = []
        self.envelopes: list[tuple[str, list[str]]] = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.wfile.write(b"220 sink ready\r\n")
                sender, recipients = "", []
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    command = line.decode(errors="replace").strip()
                    upper = command.upper()
                    if upper.startswith("EHLO") or upper.startswith("HELO"):
                        self.wfile.write(b"250 sink\r\n")
                    elif upper.startswith("MAIL FROM"):
                        sender = command.split(":", 1)[1].strip().strip("<>")
                        self.wfile.write(b"250 ok\r\n")
                    elif upper.startswith("RCPT TO"):
                        recipients.append(command.split(":", 1)[1].strip().strip("<>"))
                        self.wfile.write(b"250 ok\r\n")
                    elif upper == "DATA":
                        self.wfile.write(b"354 go\r\n")
                        data = b""
                        while True:
                            chunk = self.rfile.readline()
                            if chunk == b".\r\n":
                                break
                            data += chunk
                        sink.messages.append(data)
                        sink.envelopes.append((sender, recipients))
                        self.wfile.write(b"250 queued\r\n")
                    elif upper == "QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 ok\r\n")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        import threading

        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_a_message_survives_a_real_smtp_conversation_intact() -> None:
    sink = _SmtpSink()
    try:
        sender = SmtpEmailSender(
            host="127.0.0.1",
            port=sink.port,
            username=None,
            password=None,
            security="none",
            timeout_seconds=5,
        )
        result = sender.send(
            message(
                subject="Llevas el 80 % de tu presupuesto «Mercado»",
                html_body="<p>Hola <b>mundo</b> <img src='cid:logo'></p>",
                text_body="Hola mundo",
                headers={"List-Unsubscribe": "<https://x.example/u?t=1>"},
            )
        )
    finally:
        sink.close()

    assert sink.envelopes == [("avisos@finance.example.com", ["person@example.com"])]
    received = message_from_bytes(sink.messages[0], policy=policy.default)
    assert received["Subject"] == "Llevas el 80 % de tu presupuesto «Mercado»"
    assert received["Message-ID"] == f"<{result.provider_message_id}>"
    assert received["List-Unsubscribe"] == "<https://x.example/u?t=1>"
    assert received.get_body(("plain",)).get_content().strip() == "Hola mundo"  # type: ignore[union-attr]
    html_body = received.get_body(("html",))
    assert "<b>mundo</b>" in html_body.get_content()  # type: ignore[union-attr]
    images = [part for part in received.walk() if part.get_content_type() == "image/png"]
    assert len(images) == 1 and images[0]["Content-ID"] == "<logo>"
    assert images[0].get_content() == PNG

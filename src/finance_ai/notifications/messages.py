from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


class EmailSendError(Exception):
    """A stable, provider-neutral failure. ``code`` is safe to store and log; the provider's own
    text, the recipient and the message are never kept."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class InlineImage:
    """An image shown inside the HTML through ``cid:<content_id>`` (clients that block remote
    images and ``data:`` URIs still render it)."""

    content_id: str
    content: bytes
    mime_type: str = "image/png"
    filename: str = "logo.png"


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to_address: str
    subject: str
    html_body: str
    text_body: str
    from_address: str
    from_name: str
    reply_to: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    inline_images: tuple[InlineImage, ...] = ()


@dataclass(frozen=True, slots=True)
class SendResult:
    """``provider_message_id`` is the RFC 5322 Message-ID we generated, without angle brackets."""

    provider_message_id: str

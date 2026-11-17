"""Sends the notifications waiting in the outbox.

One row is claimed at a time with ``FOR UPDATE SKIP LOCKED`` (so several instances can run this
at once without sending a row twice), sent outside any database transaction, and its result is
recorded afterwards. A row whose instance died while sending is put back after a timeout, so
delivery is at-least-once.

Nothing here logs an address, a message body or a provider's own text: results are stable codes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.core.config import Settings
from finance_ai.core.logging import get_logger
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.notifications import NotificationOutbox, NotificationPreference
from finance_ai.notifications.messages import EmailMessage, EmailSendError
from finance_ai.notifications.rendering import (
    RenderedEmail,
    render_budget_alert,
    render_test_email,
)
from finance_ai.notifications.senders import EmailSender

logger = get_logger(__name__)

BACKOFF_MINUTES = (1, 5, 15, 60, 240)
STALE_SENDING_MINUTES = 10

Clock = Callable[[], datetime]
UnsubscribeUrlFactory = Callable[[int], str | None]


@dataclass(frozen=True, slots=True)
class DispatchSummary:
    sent: int = 0
    skipped: int = 0
    retried: int = 0
    failed: int = 0

    def plus(self, outcome: str) -> DispatchSummary:
        fields = {"sent": self.sent, "skipped": self.skipped, "retried": self.retried}
        fields["failed"] = self.failed
        fields[outcome] += 1
        return DispatchSummary(**fields)


@dataclass(frozen=True, slots=True)
class _Claimed:
    id: int
    user_id: int
    kind: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class _Recipient:
    email: str
    is_active: bool
    verified: bool
    email_enabled: bool
    budget_alerts: bool
    show_amounts: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


class NotificationDispatcher:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        sender: EmailSender,
        settings: Settings,
        clock: Clock = _utc_now,
        unsubscribe_url: UnsubscribeUrlFactory = lambda _user_id: None,
    ) -> None:
        self._factory = session_factory
        self._sender = sender
        self._settings = settings
        self._clock = clock
        self._unsubscribe_url = unsubscribe_url

    def dispatch_once(self, *, limit: int = 25, user_id: int | None = None) -> DispatchSummary:
        """Send what is due. ``user_id`` narrows the sweep to one person's notifications (used
        when that person asks for a test email and is waiting for the answer)."""
        self._recover_stale()
        summary = DispatchSummary()
        for _ in range(limit):
            claimed = self._claim_next(user_id)
            if claimed is None:
                break
            summary = summary.plus(self._process(claimed))
        return summary

    # --- claiming -------------------------------------------------------------------------

    def _recover_stale(self) -> None:
        cutoff = self._clock() - timedelta(minutes=STALE_SENDING_MINUTES)
        with self._factory() as session, session.begin():
            session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.status == "sending", NotificationOutbox.claimed_at < cutoff
                )
                .values(status="pending", claimed_at=None)
            )

    def _claim_next(self, user_id: int | None) -> _Claimed | None:
        now = self._clock()
        due = select(NotificationOutbox).where(
            NotificationOutbox.status == "pending",
            NotificationOutbox.next_attempt_at <= now,
        )
        if user_id is not None:
            due = due.where(NotificationOutbox.user_id == user_id)
        with self._factory() as session, session.begin():
            row = session.execute(
                due.order_by(NotificationOutbox.id).limit(1).with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = "sending"
            row.claimed_at = now
            row.attempts += 1
            return _Claimed(
                id=row.id,
                user_id=row.user_id,
                kind=row.kind,
                payload=dict(row.payload),
                attempts=row.attempts,
            )

    # --- one notification -----------------------------------------------------------------

    def _process(self, claimed: _Claimed) -> str:
        recipient = self._load_recipient(claimed.user_id)
        reason = self._skip_reason(claimed, recipient)
        if reason is not None or recipient is None:
            self._record(claimed, status="skipped", code=reason or "user_missing")
            return self._log(claimed, "skipped", reason or "user_missing")
        try:
            rendered = self._render(claimed, recipient)
            result = self._sender.send(self._message(claimed, recipient, rendered))
        except EmailSendError as error:
            return self._handle_failure(claimed, error.code, retryable=error.retryable)
        except Exception:
            # Unknown failures are retried but never described: the text could hold an address.
            return self._handle_failure(claimed, "send_failed", retryable=True)
        self._record(
            claimed, status="sent", code=None, provider_message_id=result.provider_message_id
        )
        return self._log(claimed, "sent", "ok")

    def _load_recipient(self, user_id: int) -> _Recipient | None:
        with self._factory() as session:
            user = session.execute(
                select(AppUser.email, AppUser.is_active, AppUser.email_verified_at).where(
                    AppUser.id == user_id
                )
            ).one_or_none()
            if user is None:
                return None
            preference = session.execute(
                select(
                    NotificationPreference.email_enabled,
                    NotificationPreference.budget_alerts,
                    NotificationPreference.show_amounts,
                ).where(NotificationPreference.user_id == user_id)
            ).one_or_none()
        return _Recipient(
            email=user.email,
            is_active=user.is_active,
            verified=user.email_verified_at is not None,
            email_enabled=bool(preference.email_enabled) if preference else False,
            budget_alerts=bool(preference.budget_alerts) if preference else True,
            show_amounts=bool(preference.show_amounts) if preference else True,
        )

    def _skip_reason(self, claimed: _Claimed, recipient: _Recipient | None) -> str | None:
        """Checked again at send time: the person may have changed their mind, or the account may
        have been suspended, after the notification was decided."""
        if recipient is None:
            return "user_missing"
        if not recipient.is_active:
            return "user_inactive"
        if not recipient.verified:
            return "email_not_verified"
        if claimed.kind == "budget_alert":
            if not recipient.email_enabled:
                return "notifications_disabled"
            if not recipient.budget_alerts:
                return "budget_alerts_disabled"
        if self._sent_in_last_day(claimed.user_id) >= self._settings.email_daily_cap_per_user:
            return "daily_cap"
        return None

    def _sent_in_last_day(self, user_id: int) -> int:
        since = self._clock() - timedelta(hours=24)
        with self._factory() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(NotificationOutbox)
                    .where(
                        NotificationOutbox.user_id == user_id,
                        NotificationOutbox.status == "sent",
                        NotificationOutbox.sent_at >= since,
                    )
                )
                or 0
            )

    def _render(self, claimed: _Claimed, recipient: _Recipient) -> RenderedEmail:
        unsubscribe = self._unsubscribe_url(claimed.user_id)
        if claimed.kind == "test":
            return render_test_email(unsubscribe_url=unsubscribe)
        payload = claimed.payload
        return render_budget_alert(
            budget_name=str(payload["budget_name"]),
            period_start=date.fromisoformat(str(payload["period_start"])),
            period_end=date.fromisoformat(str(payload["period_end"])),
            threshold=int(payload["threshold"]),
            spent=Decimal(str(payload["spent"])),
            limit=Decimal(str(payload["limit"])),
            currency_code=str(payload["currency_code"]),
            show_amounts=recipient.show_amounts,
            unsubscribe_url=unsubscribe,
        )

    def _message(
        self, claimed: _Claimed, recipient: _Recipient, rendered: RenderedEmail
    ) -> EmailMessage:
        settings = self._settings
        headers: dict[str, str] = {}
        unsubscribe = self._unsubscribe_url(claimed.user_id)
        if unsubscribe is not None:
            headers["List-Unsubscribe"] = f"<{unsubscribe}>"
            headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        return EmailMessage(
            to_address=recipient.email,
            subject=rendered.subject,
            html_body=rendered.html,
            text_body=rendered.text,
            from_address=settings.email_from_address or "no-reply@finance-ai.invalid",
            from_name=settings.email_from_name,
            reply_to=settings.email_reply_to,
            headers=headers,
            inline_images=rendered.inline_images,
        )

    # --- recording ------------------------------------------------------------------------

    def _handle_failure(self, claimed: _Claimed, code: str, *, retryable: bool) -> str:
        if retryable and claimed.attempts < self._settings.email_max_attempts:
            delay = BACKOFF_MINUTES[min(claimed.attempts, len(BACKOFF_MINUTES)) - 1]
            self._record(
                claimed,
                status="pending",
                code=code,
                next_attempt_at=self._clock() + timedelta(minutes=delay),
            )
            return self._log(claimed, "retried", code)
        self._record(claimed, status="failed", code=code)
        return self._log(claimed, "failed", code)

    def _record(
        self,
        claimed: _Claimed,
        *,
        status: str,
        code: str | None,
        provider_message_id: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "last_error_code": code,
            "claimed_at": None,
            "provider_message_id": provider_message_id,
        }
        if status == "sent":
            values["sent_at"] = self._clock()
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        with self._factory() as session, session.begin():
            session.execute(
                update(NotificationOutbox)
                .where(NotificationOutbox.id == claimed.id, NotificationOutbox.status == "sending")
                .values(**values)
            )

    @staticmethod
    def _log(claimed: _Claimed, outcome: str, code: str) -> str:
        logger.info(
            "Notification dispatched",
            extra={
                "event": "notification.dispatch",
                "operation": claimed.kind,
                "outcome": f"{outcome}:{code}",
            },
        )
        return outcome

"""What a person controls about email: verifying the address, the preferences and a test send.

The six-digit code is sent straight from the request, never through the outbox, so the code is
never stored in plain text anywhere. Everything else about delivery (retries, the daily cap, the
re-check of consent at send time) stays in the dispatcher.

Addresses are read here only to be handed to the sender or masked; nothing logs them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, lazyload, sessionmaker

from finance_ai.core.config import Settings
from finance_ai.core.errors import AppError
from finance_ai.core.logging import get_logger
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.notifications import (
    EmailVerification,
    NotificationOutbox,
    NotificationPreference,
)
from finance_ai.notifications.dispatcher import NotificationDispatcher
from finance_ai.notifications.messages import EmailMessage, EmailSendError
from finance_ai.notifications.outbox import enqueue
from finance_ai.notifications.rendering import render_verification_code
from finance_ai.notifications.senders import (
    EmailSender,
    NotificationsUnavailableError,
    delivery_available,
)
from finance_ai.notifications.tokens import (
    codes_match,
    hash_verification_code,
    new_verification_code,
)

logger = get_logger(__name__)

RESEND_COOLDOWN_SECONDS = 60
CODES_PER_HOUR = 5
TEST_EMAILS_PER_HOUR = 3

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """Timestamps come back timezone-aware from PostgreSQL; treat a naive one as UTC rather than
    failing a comparison (SQLite, used by the unit tests, drops the zone)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class PreferencesView:
    email_hint: str
    email_verified: bool
    email_enabled: bool
    budget_alerts: bool
    show_amounts: bool
    delivery_available: bool


@dataclass(frozen=True, slots=True)
class VerificationIssued:
    email_hint: str
    expires_in_seconds: int
    resend_in_seconds: int


@dataclass(frozen=True, slots=True)
class TestEmailResult:
    status: Literal["sent", "queued"]


def mask_email(address: str) -> str:
    """``maria.lopez@example.com`` -> ``m***@example.com``: enough for the person to recognise
    their own address on screen, not enough to be useful if a screenshot leaks."""
    local, _, domain = address.partition("@")
    if not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


class AccountNotificationError(AppError):
    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            status=status,
            code=code,
            title=title,
            detail=detail,
            headers={"Retry-After": str(retry_after)} if retry_after is not None else None,
        )


def _not_verified() -> AccountNotificationError:
    return AccountNotificationError(
        status=409,
        code="email_not_verified",
        title="The email address is not verified",
        detail="Verify your email address before turning on email notifications.",
    )


def _invalid_code() -> AccountNotificationError:
    return AccountNotificationError(
        status=400,
        code="verification_code_invalid",
        title="The code is not valid",
        detail="That code is not right, or there is no pending code. Request a new one.",
    )


class NotificationAccountService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        sender_factory: Callable[[], EmailSender],
        dispatcher_factory: Callable[[], NotificationDispatcher],
        settings: Settings,
        clock: Clock = _utc_now,
    ) -> None:
        self._factory = session_factory
        self._sender_factory = sender_factory
        self._dispatcher_factory = dispatcher_factory
        self._settings = settings
        self._clock = clock

    # --- preferences ----------------------------------------------------------------------

    def get_preferences(self, user_id: int) -> PreferencesView:
        with self._factory() as session:
            return self._view(session, user_id)

    def update_preferences(
        self,
        user_id: int,
        *,
        email_enabled: bool | None,
        budget_alerts: bool | None,
        show_amounts: bool | None,
    ) -> PreferencesView:
        with self._factory() as session, session.begin():
            user = self._load_user(session, user_id)
            if email_enabled:
                if user.email_verified_at is None:
                    raise _not_verified()
                if not delivery_available(self._settings):
                    raise NotificationsUnavailableError()
            preference = self._locked_preference(session, user_id)
            if email_enabled is not None:
                preference.email_enabled = email_enabled
            if budget_alerts is not None:
                preference.budget_alerts = budget_alerts
            if show_amounts is not None:
                preference.show_amounts = show_amounts
            preference.updated_at = self._clock()
        return self.get_preferences(user_id)

    def unsubscribe(self, user_id: int) -> bool:
        """Turn email notifications off. Idempotent; returns whether the person exists."""
        with self._factory() as session, session.begin():
            if session.scalar(select(AppUser.id).where(AppUser.id == user_id)) is None:
                return False
            preference = self._locked_preference(session, user_id)
            preference.email_enabled = False
            preference.updated_at = self._clock()
        logger.info(
            "Email notifications turned off",
            extra={"event": "notification.unsubscribe", "outcome": "ok"},
        )
        return True

    # --- verification ---------------------------------------------------------------------

    def request_verification(self, user_id: int) -> VerificationIssued:
        if not delivery_available(self._settings):
            raise NotificationsUnavailableError()
        now = self._clock()
        code = new_verification_code()
        expires = timedelta(minutes=self._settings.email_verification_minutes)
        previous_ids: list[int] = []  # the code this request replaces, if any
        try:
            with self._factory() as session, session.begin():
                user = self._load_user(session, user_id)
                if user.email_verified_at is not None:
                    raise AccountNotificationError(
                        status=409,
                        code="email_already_verified",
                        title="The email address is already verified",
                        detail="This email address is already verified.",
                    )
                address = user.email
                self._enforce_code_limits(session, user_id, now)
                previous_ids = list(
                    session.scalars(
                        update(EmailVerification)
                        .where(
                            EmailVerification.user_id == user_id,
                            EmailVerification.consumed_at.is_(None),
                        )
                        .values(consumed_at=now)
                        .returning(EmailVerification.id)
                    )
                )
                verification = EmailVerification(
                    user_id=user_id,
                    code_hash=hash_verification_code(self._settings, user_id=user_id, code=code),
                    expires_at=now + expires,
                    created_at=now,
                )
                session.add(verification)
                session.flush()
                verification_id = verification.id
        except IntegrityError:
            # Two requests raced for the single open code: the other one won.
            raise self._cooldown(RESEND_COOLDOWN_SECONDS) from None

        rendered = render_verification_code(
            code=code, minutes=self._settings.email_verification_minutes
        )
        message = EmailMessage(
            to_address=address,
            subject=rendered.subject,
            html_body=rendered.html,
            text_body=rendered.text,
            from_address=self._settings.email_from_address or "no-reply@finance-ai.invalid",
            from_name=self._settings.email_from_name,
            reply_to=self._settings.email_reply_to,
            inline_images=rendered.inline_images,
        )
        try:
            self._sender_factory().send(message)
        except EmailSendError as error:
            self._discard_verification(verification_id, restore=previous_ids)
            logger.warning(
                "Verification email not sent",
                extra={
                    "event": "notification.verification",
                    "outcome": f"failed:{error.code}",
                },
            )
            raise AccountNotificationError(
                status=503,
                code="email_delivery_failed",
                title="The email could not be sent",
                detail="We could not send the code right now. Try again in a few minutes.",
            ) from None
        logger.info(
            "Verification email sent",
            extra={"event": "notification.verification", "outcome": "sent"},
        )
        return VerificationIssued(
            email_hint=mask_email(address),
            expires_in_seconds=int(expires.total_seconds()),
            resend_in_seconds=RESEND_COOLDOWN_SECONDS,
        )

    def confirm_verification(self, user_id: int, code: str) -> PreferencesView:
        now = self._clock()
        presented = hash_verification_code(self._settings, user_id=user_id, code=code)
        failure: AccountNotificationError | None = None
        with self._factory() as session, session.begin():
            row = session.execute(
                select(EmailVerification)
                .where(
                    EmailVerification.user_id == user_id,
                    EmailVerification.consumed_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise _invalid_code()
            if _aware(row.expires_at) <= now:
                row.consumed_at = now
                failure = AccountNotificationError(
                    status=400,
                    code="verification_code_expired",
                    title="The code has expired",
                    detail="That code has expired. Request a new one.",
                )
            elif not codes_match(row.code_hash, presented):
                row.attempts += 1
                if row.attempts >= self._settings.email_verification_max_attempts:
                    row.consumed_at = now
                    failure = AccountNotificationError(
                        status=429,
                        code="verification_attempts_exceeded",
                        title="Too many wrong codes",
                        detail="Too many wrong codes. Request a new one.",
                    )
                else:
                    failure = _invalid_code()
            else:
                row.consumed_at = now
                user = self._load_user(session, user_id)
                user.email_verified_at = now
                preference = self._locked_preference(session, user_id)
                preference.email_enabled = True
                preference.updated_at = now
        if failure is not None:
            # The attempt counter was committed above; only now is the failure reported.
            raise failure
        logger.info(
            "Email address verified",
            extra={"event": "notification.verification", "outcome": "verified"},
        )
        return self.get_preferences(user_id)

    # --- test email -----------------------------------------------------------------------

    def send_test_email(self, user_id: int) -> TestEmailResult:
        if not delivery_available(self._settings):
            raise NotificationsUnavailableError()
        now = self._clock()
        with self._factory() as session, session.begin():
            user = self._load_user(session, user_id)
            if user.email_verified_at is None:
                raise _not_verified()
            recent = int(
                session.scalar(
                    select(func.count())
                    .select_from(NotificationOutbox)
                    .where(
                        NotificationOutbox.user_id == user_id,
                        NotificationOutbox.kind == "test",
                        NotificationOutbox.created_at >= now - timedelta(hours=1),
                    )
                )
                or 0
            )
            if recent >= TEST_EMAILS_PER_HOUR:
                raise AccountNotificationError(
                    status=429,
                    code="test_email_rate_limited",
                    title="Too many test emails",
                    detail="You asked for several test emails already. Try again later.",
                    retry_after=600,
                )
            dedupe_key = f"test:{user_id}:{uuid4().hex}"
            enqueue(
                session,
                user_id=user_id,
                kind="test",
                dedupe_key=dedupe_key,
                payload={},
                not_before=now,
            )
        self._dispatcher_factory().dispatch_once(limit=5, user_id=user_id)
        with self._factory() as session:
            status = session.scalar(
                select(NotificationOutbox.status).where(NotificationOutbox.dedupe_key == dedupe_key)
            )
        if status in ("failed", "skipped"):
            # Final outcomes: telling the person it is "queued" would promise mail that never comes.
            raise AccountNotificationError(
                status=503,
                code="email_delivery_failed",
                title="The test email could not be sent",
                detail="We could not send the test email. Try again in a few minutes.",
            )
        return TestEmailResult(status="sent" if status == "sent" else "queued")

    # --- helpers --------------------------------------------------------------------------

    def _view(self, session: Session, user_id: int) -> PreferencesView:
        user = self._load_user(session, user_id)
        preference = session.execute(
            select(
                NotificationPreference.email_enabled,
                NotificationPreference.budget_alerts,
                NotificationPreference.show_amounts,
            ).where(NotificationPreference.user_id == user_id)
        ).one_or_none()
        return PreferencesView(
            email_hint=mask_email(user.email),
            email_verified=user.email_verified_at is not None,
            email_enabled=bool(preference.email_enabled) if preference else False,
            budget_alerts=bool(preference.budget_alerts) if preference else True,
            show_amounts=bool(preference.show_amounts) if preference else True,
            delivery_available=delivery_available(self._settings),
        )

    @staticmethod
    def _load_user(session: Session, user_id: int) -> AppUser:
        # AppUser joins its role and currency eagerly; a row lock on that join is not allowed.
        user = session.execute(
            select(AppUser).options(lazyload("*")).where(AppUser.id == user_id)
        ).scalar_one_or_none()
        if user is None:
            raise AccountNotificationError(
                status=404,
                code="account_not_found",
                title="Account not found",
                detail="The account could not be found.",
            )
        return user

    @staticmethod
    def _locked_preference(session: Session, user_id: int) -> NotificationPreference:
        """The person's preference row, created with the defaults on first use. The insert ignores
        a concurrent twin and the select then locks whichever row won."""
        dialect = session.get_bind().dialect.name
        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
        session.execute(
            insert(NotificationPreference)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        return session.execute(
            select(NotificationPreference)
            .where(NotificationPreference.user_id == user_id)
            .with_for_update()
        ).scalar_one()

    def _enforce_code_limits(self, session: Session, user_id: int, now: datetime) -> None:
        newest = session.scalar(
            select(func.max(EmailVerification.created_at)).where(
                EmailVerification.user_id == user_id
            )
        )
        if newest is not None:
            wait = int(
                (_aware(newest) + timedelta(seconds=RESEND_COOLDOWN_SECONDS) - now).total_seconds()
            )
            if wait > 0:
                raise self._cooldown(wait)
        issued = int(
            session.scalar(
                select(func.count())
                .select_from(EmailVerification)
                .where(
                    EmailVerification.user_id == user_id,
                    EmailVerification.created_at >= now - timedelta(hours=1),
                )
            )
            or 0
        )
        if issued >= CODES_PER_HOUR:
            raise AccountNotificationError(
                status=429,
                code="verification_rate_limited",
                title="Too many codes requested",
                detail="You asked for several codes already. Try again later.",
                retry_after=900,
            )

    @staticmethod
    def _cooldown(seconds: int) -> AccountNotificationError:
        return AccountNotificationError(
            status=429,
            code="verification_cooldown",
            title="Please wait before asking for another code",
            detail="A code was just sent. Wait a moment before requesting another.",
            retry_after=max(seconds, 1),
        )

    def _discard_verification(self, verification_id: int, *, restore: list[int]) -> None:
        """A code that never left the building must not count against the person's limits, and
        must not cost them the earlier code that is still in their inbox: reopen it if it has
        not expired."""
        with self._factory() as session, session.begin():
            session.execute(
                delete(EmailVerification).where(EmailVerification.id == verification_id)
            )
            if restore:
                session.execute(
                    update(EmailVerification)
                    .where(
                        EmailVerification.id.in_(restore),
                        EmailVerification.expires_at > self._clock(),
                    )
                    .values(consumed_at=None)
                )

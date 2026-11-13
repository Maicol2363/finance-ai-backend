from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.passwords import PasswordHasher
from finance_ai.auth.session_policy import Clock
from finance_ai.core.errors import (
    AuditUnavailableError,
    StaffAccessDeniedError,
    StaffAuthenticationRequiredError,
    StaffLoginInvalidError,
    StaffSessionExistsError,
    StaffSessionExpiredError,
    StaffSessionInvalidError,
)
from finance_ai.db.models.admin import StaffInvitation
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import RenewableCredential, UserSession
from finance_ai.staff_auth.schemas import StaffLoginCommand, StaffSessionUser


class StaffSessionService:
    """Application service for server-owned staff browser sessions."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        password_hasher: PasswordHasher,
        credential_codec: CredentialCodec,
        clock: Clock,
        audit_writer: SecurityAuditWriter,
    ) -> None:
        self._session_factory = session_factory
        self._password_hasher = password_hasher
        self._credential_codec = credential_codec
        self._clock = clock
        self._audit_writer = audit_writer

    def login(
        self, command: StaffLoginCommand, *, correlation_id: str
    ) -> tuple[StaffSessionUser, str, str, datetime, datetime, datetime]:
        email = command.email.strip().lower()
        with self._session_factory() as session:
            snapshot = session.execute(
                select(AppUser.id, AppUser.password_hash, AppUser.is_active)
                .where(AppUser.email == email)
                .limit(1)
            ).one_or_none()
        password_matches = self._password_hasher.verify_real_or_dummy(
            snapshot.password_hash if snapshot is not None else None,
            command.password,
        )
        if snapshot is None or not password_matches:
            self._write_denied_login(correlation_id=correlation_id)
            raise StaffLoginInvalidError()

        now = self._clock.now()
        access = self._credential_codec.issue()
        refresh = self._credential_codec.issue()
        access_expires = now + timedelta(minutes=15)
        idle_expires = now + timedelta(minutes=30)
        absolute_expires = now + timedelta(hours=8)
        if idle_expires > absolute_expires:
            idle_expires = absolute_expires
        if access_expires > idle_expires:
            access_expires = idle_expires

        denial = False
        conflict = False
        try:
            with self._session_factory() as session, session.begin():
                user = session.execute(
                    select(AppUser, Role.name, Currency.code)
                    .join(Role, Role.id == AppUser.role_id)
                    .join(Currency, Currency.id == AppUser.preferred_currency_id)
                    .where(AppUser.id == snapshot.id)
                    .with_for_update(of=AppUser)
                ).one_or_none()
                if user is None:
                    denial = True
                    self._audit_writer.append(
                        session,
                        event_type="staff.login_denied",
                        actor_id=None,
                        result="denied",
                        correlation_id=correlation_id,
                        context={"reason": "ineligible"},
                    )
                else:
                    account, role_name, _currency_code = user
                    eligible = self._activation_is_authoritative(session, account)
                    if (
                        not account.is_active
                        or account.password_hash != snapshot.password_hash
                        or role_name != "support_agent"
                        or not eligible
                    ):
                        denial = True
                        self._audit_writer.append(
                            session,
                            event_type="staff.login_denied",
                            actor_id=str(account.id),
                            result="denied",
                            correlation_id=correlation_id,
                            context={"reason": "ineligible"},
                        )
                    else:
                        active_ids = list(
                            session.scalars(
                                select(UserSession.id)
                                .where(
                                    UserSession.user_id == account.id,
                                    UserSession.platform == "staff_browser",
                                    UserSession.revoked_at.is_(None),
                                    UserSession.idle_expires_at > now,
                                    UserSession.absolute_expires_at > now,
                                )
                                .with_for_update()
                            )
                        )
                        if active_ids:
                            conflict = True
                            self._audit_writer.append(
                                session,
                                event_type="staff.login_denied",
                                actor_id=str(account.id),
                                result="denied",
                                correlation_id=correlation_id,
                                context={"reason": "session_exists"},
                            )
                        else:
                            browser_session = UserSession(
                                user_id=account.id,
                                platform="staff_browser",
                                access_token_hash=access.token_hash,
                                access_expires_at=access_expires,
                                idle_expires_at=idle_expires,
                                absolute_expires_at=absolute_expires,
                                last_renewed_at=now,
                            )
                            session.add(browser_session)
                            session.flush()
                            session.add(
                                RenewableCredential(
                                    session_id=browser_session.id,
                                    token_hash=refresh.token_hash,
                                    issued_at=now,
                                    expires_at=idle_expires,
                                )
                            )
                            self._audit_writer.append(
                                session,
                                event_type="staff.login",
                                actor_id=str(account.id),
                                result="success",
                                correlation_id=correlation_id,
                                context={},
                            )
                            view = StaffSessionUser(
                                id=account.id, email=account.email, role=role_name
                            )
        except Exception as exc:
            if isinstance(exc, (StaffLoginInvalidError, StaffSessionExistsError)):
                raise
            raise AuditUnavailableError() from exc

        if conflict:
            raise StaffSessionExistsError()
        if denial:
            raise StaffLoginInvalidError()
        return view, access.raw, refresh.raw, access_expires, idle_expires, absolute_expires

    def resolve(
        self, raw_access: str | None, *, correlation_id: str = "unavailable"
    ) -> StaffSessionUser:
        return self.session_projection(raw_access, correlation_id=correlation_id)[0]

    def session_projection(
        self, raw_access: str | None, *, correlation_id: str = "unavailable"
    ) -> tuple[StaffSessionUser, datetime, datetime, datetime]:
        try:
            return self._session_projection(raw_access, correlation_id=correlation_id)
        except (
            StaffAccessDeniedError,
            StaffAuthenticationRequiredError,
            StaffSessionExpiredError,
            StaffSessionInvalidError,
        ):
            raise
        except Exception as exc:
            raise AuditUnavailableError() from exc

    def _session_projection(
        self, raw_access: str | None, *, correlation_id: str
    ) -> tuple[StaffSessionUser, datetime, datetime, datetime]:
        token_hash = self._credential_codec.try_hash_presented(raw_access or "")
        if token_hash is None:
            raise StaffAuthenticationRequiredError()
        now = self._clock.now()
        denied = False
        projection: tuple[StaffSessionUser, datetime, datetime, datetime] | None = None
        with self._session_factory() as session, session.begin():
            row = session.execute(
                select(UserSession, AppUser, Role.name)
                .join(AppUser, UserSession.user_id == AppUser.id)
                .join(Role, Role.id == AppUser.role_id)
                .where(
                    UserSession.access_token_hash == token_hash,
                    UserSession.platform == "staff_browser",
                )
                .with_for_update(of=UserSession)
            ).one_or_none()
            if row is None:
                raise StaffSessionInvalidError()
            auth_session, account, role_name = row
            if auth_session.revoked_at is not None:
                raise StaffSessionInvalidError()
            access_expires_at = _utc(auth_session.access_expires_at)
            idle_expires_at = _utc(auth_session.idle_expires_at)
            absolute_expires_at = _utc(auth_session.absolute_expires_at)
            if access_expires_at <= now:
                raise StaffSessionExpiredError()
            if idle_expires_at <= now or absolute_expires_at <= now:
                raise StaffSessionInvalidError()
            if (
                not account.is_active
                or role_name != "support_agent"
                or not self._activation_is_authoritative(session, account)
            ):
                denied = True
                auth_session.revoked_at = now
                auth_session.revocation_reason = "authority_changed"
                self._audit_writer.append(
                    session,
                    event_type="staff.session_revoked",
                    actor_id=str(account.id),
                    target_id=str(auth_session.id),
                    result="denied",
                    correlation_id=correlation_id,
                    context={"reason": "authority_changed"},
                )
            else:
                projection = (
                    StaffSessionUser(id=account.id, email=account.email, role=role_name),
                    access_expires_at,
                    idle_expires_at,
                    absolute_expires_at,
                )
        if denied:
            raise StaffAccessDeniedError()
        if projection is None:
            raise StaffSessionInvalidError()
        return projection

    def refresh(
        self, raw_refresh: str | None, *, correlation_id: str
    ) -> tuple[StaffSessionUser, str, str, datetime, datetime, datetime]:
        token_hash = self._credential_codec.try_hash_presented(raw_refresh or "")
        if token_hash is None:
            raise StaffSessionInvalidError()
        now = self._clock.now()
        access = self._credential_codec.issue()
        replacement = self._credential_codec.issue()
        denial = False
        replay_event: tuple[str, str] | None = None
        result: tuple[StaffSessionUser, str, str, datetime, datetime, datetime] | None = None
        try:
            with self._session_factory() as session, session.begin():
                credential = session.execute(
                    select(RenewableCredential)
                    .where(RenewableCredential.token_hash == token_hash)
                    .with_for_update()
                ).scalar_one_or_none()
                if credential is None:
                    raise StaffSessionInvalidError()
                browser_session = session.execute(
                    select(UserSession, AppUser, Role.name)
                    .join(AppUser, AppUser.id == UserSession.user_id)
                    .join(Role, Role.id == AppUser.role_id)
                    .where(UserSession.id == credential.session_id)
                    .with_for_update(of=UserSession)
                ).one_or_none()
                if browser_session is None:
                    raise StaffSessionInvalidError()
                auth_session, account, role_name = browser_session
                if auth_session.platform != "staff_browser":
                    raise StaffSessionInvalidError()
                if credential.consumed_at is not None:
                    if auth_session.revoked_at is None:
                        auth_session.revoked_at = now
                        auth_session.revocation_reason = "replay"
                    replay_event = (str(account.id), str(auth_session.id))
                elif (
                    auth_session.revoked_at is not None
                    or _utc(credential.expires_at) <= now
                    or _utc(auth_session.idle_expires_at) <= now
                    or _utc(auth_session.absolute_expires_at) <= now
                ):
                    denial = True
                elif (
                    not account.is_active
                    or role_name != "support_agent"
                    or not self._activation_is_authoritative(session, account)
                ):
                    denial = True
                    auth_session.revoked_at = now
                    auth_session.revocation_reason = "authority_changed"
                    self._audit_writer.append(
                        session,
                        event_type="staff.session_revoked",
                        actor_id=str(account.id),
                        target_id=str(auth_session.id),
                        result="denied",
                        correlation_id=correlation_id,
                        context={"reason": "authority_changed"},
                    )
                else:
                    absolute_expires = _utc(auth_session.absolute_expires_at)
                    idle_expires = min(now + timedelta(minutes=30), absolute_expires)
                    access_expires = min(now + timedelta(minutes=15), idle_expires)
                    credential.consumed_at = now
                    auth_session.access_token_hash = access.token_hash
                    auth_session.access_expires_at = access_expires
                    auth_session.idle_expires_at = idle_expires
                    auth_session.last_renewed_at = now
                    session.add(
                        RenewableCredential(
                            session_id=auth_session.id,
                            token_hash=replacement.token_hash,
                            issued_at=now,
                            expires_at=idle_expires,
                        )
                    )
                    self._audit_writer.append(
                        session,
                        event_type="staff.refresh",
                        actor_id=str(account.id),
                        target_id=str(auth_session.id),
                        result="success",
                        correlation_id=correlation_id,
                        context={},
                    )
                    result = (
                        StaffSessionUser(id=account.id, email=account.email, role=role_name),
                        access.raw,
                        replacement.raw,
                        access_expires,
                        idle_expires,
                        absolute_expires,
                    )
        except StaffSessionInvalidError:
            raise
        except Exception as exc:
            raise AuditUnavailableError() from exc
        if replay_event is not None:
            try:
                with self._session_factory() as audit_session, audit_session.begin():
                    self._audit_writer.append(
                        audit_session,
                        event_type="staff.session_revoked",
                        actor_id=replay_event[0],
                        target_id=replay_event[1],
                        result="denied",
                        correlation_id=correlation_id,
                        context={"reason": "refresh_replay"},
                    )
            except Exception as exc:
                raise AuditUnavailableError() from exc
            raise StaffSessionInvalidError()
        if denial:
            raise StaffAccessDeniedError()
        if result is None:
            raise StaffSessionInvalidError()
        return result

    def logout(
        self, raw_refresh: str | None, raw_access: str | None, *, correlation_id: str
    ) -> None:
        refresh_hash = self._credential_codec.try_hash_presented(raw_refresh or "")
        access_hash = self._credential_codec.try_hash_presented(raw_access or "")
        now = self._clock.now()
        try:
            with self._session_factory() as session, session.begin():
                browser_session = None
                actor_id: str | None = None
                if refresh_hash is not None:
                    credential = session.execute(
                        select(RenewableCredential)
                        .where(RenewableCredential.token_hash == refresh_hash)
                        .with_for_update()
                    ).scalar_one_or_none()
                    if credential is not None:
                        browser_session = session.execute(
                            select(UserSession, AppUser.id)
                            .join(AppUser, AppUser.id == UserSession.user_id)
                            .where(UserSession.id == credential.session_id)
                            .with_for_update(of=UserSession)
                        ).one_or_none()
                if (
                    browser_session is None
                    or browser_session[0].platform != "staff_browser"
                    or browser_session[0].revoked_at is not None
                ) and access_hash is not None:
                    browser_session = session.execute(
                        select(UserSession, AppUser.id)
                        .join(AppUser, AppUser.id == UserSession.user_id)
                        .where(UserSession.access_token_hash == access_hash)
                        .with_for_update(of=UserSession)
                    ).one_or_none()
                if browser_session is not None:
                    auth_session, user_id = browser_session
                    if auth_session.platform == "staff_browser" and auth_session.revoked_at is None:
                        actor_id = str(user_id)
                        auth_session.revoked_at = now
                        auth_session.revocation_reason = "logout"
                        self._audit_writer.append(
                            session,
                            event_type="staff.logout",
                            actor_id=actor_id,
                            target_id=str(auth_session.id),
                            result="success",
                            correlation_id=correlation_id,
                            context={},
                        )
        except Exception as exc:
            raise AuditUnavailableError() from exc

    @staticmethod
    def _activation_is_authoritative(session: Session, user: AppUser) -> bool:
        return bool(
            session.scalar(
                select(StaffInvitation.id)
                .join(Role, Role.id == StaffInvitation.role_id)
                .where(
                    StaffInvitation.activated_user_id == user.id,
                    StaffInvitation.consumed_at.is_not(None),
                    StaffInvitation.email == user.email,
                    StaffInvitation.role_id == user.role_id,
                    Role.name == "support_agent",
                )
                .limit(1)
            )
        )

    def _write_denied_login(self, *, correlation_id: str) -> None:
        try:
            with self._session_factory() as session, session.begin():
                self._audit_writer.append(
                    session,
                    event_type="staff.login_denied",
                    actor_id=None,
                    result="denied",
                    correlation_id=correlation_id,
                    context={"reason": "invalid_credentials"},
                )
        except Exception as exc:
            raise AuditUnavailableError() from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

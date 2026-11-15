from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, lazyload, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.credentials import CredentialCodec, IssuedCredential
from finance_ai.auth.passwords import PasswordHasher
from finance_ai.auth.session_policy import Clock
from finance_ai.core.errors import (
    AuditUnavailableError,
    MfaAlreadyEnrolledError,
    MfaChallengeInvalidError,
    MfaCodeInvalidError,
    MfaEnrollmentRequiredError,
    MfaUnavailableError,
    StaffAccessDeniedError,
    StaffAuthenticationRequiredError,
    StaffLoginInvalidError,
    StaffSessionExistsError,
    StaffSessionExpiredError,
    StaffSessionInvalidError,
)
from finance_ai.core.roles import ADMIN_ROLE, MFA_REQUIRED_ROLES, STAFF_PORTAL_ROLES
from finance_ai.db.models.admin import AdminTotpFactor, StaffInvitation, StaffMfaChallenge
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.reference import Currency
from finance_ai.db.models.sessions import RenewableCredential, UserSession
from finance_ai.staff_auth.mfa import StaffMfaPolicy, provisioning_uri, qr_svg_data_url
from finance_ai.staff_auth.schemas import StaffLoginCommand, StaffSessionUser

StaffSessionIssue = tuple[StaffSessionUser, str, str, datetime, datetime, datetime]


@dataclass(frozen=True)
class StaffMfaChallengeIssued:
    """Password verified; a second factor is still required. Grants no staff authority."""

    raw_challenge: str
    expires_at: datetime
    enrollment_required: bool


@dataclass(frozen=True)
class StaffMfaEnrollment:
    issuer: str
    account_name: str
    secret: str
    otpauth_uri: str
    qr_svg_data_url: str


StaffLoginOutcome = StaffSessionIssue | StaffMfaChallengeIssued


@dataclass(frozen=True)
class StaffLifetimes:
    """Access, idle and absolute lifetimes of one staff browser session."""

    access: timedelta
    idle: timedelta
    absolute: timedelta


STANDARD_STAFF_LIFETIMES = StaffLifetimes(
    access=timedelta(minutes=15), idle=timedelta(minutes=30), absolute=timedelta(hours=8)
)
# SEC-01: administrators hold shorter sessions than the other staff roles.
DEFAULT_ADMIN_LIFETIMES = StaffLifetimes(
    access=timedelta(minutes=10), idle=timedelta(minutes=15), absolute=timedelta(hours=4)
)


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
        mfa: StaffMfaPolicy | None = None,
        admin_lifetimes: StaffLifetimes = DEFAULT_ADMIN_LIFETIMES,
    ) -> None:
        self._admin_lifetimes = admin_lifetimes
        self._session_factory = session_factory
        self._password_hasher = password_hasher
        self._credential_codec = credential_codec
        self._clock = clock
        self._audit_writer = audit_writer
        self._mfa = mfa

    def login(self, command: StaffLoginCommand, *, correlation_id: str) -> StaffLoginOutcome:
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
        challenge = self._credential_codec.issue()

        outcome: str | None = None
        result: StaffLoginOutcome | None = None
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
                    outcome = "denied"
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
                    eligible = self._activation_is_authoritative(session, account, role_name)
                    if (
                        not account.is_active
                        or account.password_hash != snapshot.password_hash
                        or role_name not in STAFF_PORTAL_ROLES
                        or not eligible
                    ):
                        outcome = "denied"
                        self._write_login_denied(session, account.id, "ineligible", correlation_id)
                    elif self._has_active_staff_session(session, account.id, now):
                        outcome = "conflict"
                        self._write_login_denied(
                            session, account.id, "session_exists", correlation_id
                        )
                    elif role_name in MFA_REQUIRED_ROLES:
                        if self._mfa is None:
                            outcome = "mfa_unavailable"
                            self._write_login_denied(
                                session, account.id, "mfa_unavailable", correlation_id
                            )
                        else:
                            result = self._issue_mfa_challenge(session, account.id, challenge, now)
                    else:
                        access_expires, idle_expires, absolute_expires = self._initial_expiries(
                            role_name, now
                        )
                        self._create_browser_session(
                            session,
                            account.id,
                            account.role_id,
                            access,
                            refresh,
                            (access_expires, idle_expires, absolute_expires),
                            now=now,
                            mfa_verified_at=None,
                        )
                        self._audit_writer.append(
                            session,
                            event_type="staff.login",
                            actor_id=str(account.id),
                            result="success",
                            correlation_id=correlation_id,
                            context={},
                        )
                        result = (
                            StaffSessionUser(id=account.id, email=account.email, role=role_name),
                            access.raw,
                            refresh.raw,
                            access_expires,
                            idle_expires,
                            absolute_expires,
                        )
        except Exception as exc:
            raise AuditUnavailableError() from exc

        if outcome == "conflict":
            raise StaffSessionExistsError()
        if outcome == "mfa_unavailable":
            raise MfaUnavailableError()
        if outcome == "denied" or result is None:
            raise StaffLoginInvalidError()
        return result

    def start_mfa_enrollment(
        self, raw_challenge: str | None, *, correlation_id: str
    ) -> StaffMfaEnrollment:
        """Create a pending authenticator inside a valid challenge; active only after a code."""
        policy = self._require_mfa()
        token_hash = self._credential_codec.try_hash_presented(raw_challenge or "")
        if token_hash is None:
            raise MfaChallengeInvalidError()
        now = self._clock.now()
        secret = policy.verifier.new_secret()
        outcome: str | None = None
        account_name = ""
        try:
            with self._session_factory() as session, session.begin():
                # MFA flows that touch both rows always lock AppUser before StaffMfaChallenge.
                challenge_user_id = self._mfa_challenge_user_id(session, token_hash)
                account = (
                    None
                    if challenge_user_id is None
                    else self._lock_mfa_account(session, challenge_user_id)
                )
                challenge = self._lock_open_challenge(session, token_hash, now)
                if challenge is None or challenge.user_id != challenge_user_id:
                    outcome = "challenge_invalid"
                elif account is None:
                    challenge.consumed_at = now
                    self._write_login_denied(
                        session, challenge.user_id, "ineligible", correlation_id
                    )
                    outcome = "ineligible"
                elif self._factor(session, account.id, "active") is not None:
                    outcome = "already_enrolled"
                else:
                    session.execute(
                        update(AdminTotpFactor)
                        .where(
                            AdminTotpFactor.admin_user_id == account.id,
                            AdminTotpFactor.state == "pending",
                        )
                        .values(state="disabled")
                    )
                    session.add(
                        AdminTotpFactor(
                            admin_user_id=account.id,
                            secret_ciphertext=policy.cipher.encrypt(secret, user_id=account.id),
                            state="pending",
                            created_at=now,
                        )
                    )
                    account_name = account.email
        except Exception as exc:
            raise AuditUnavailableError() from exc

        if outcome == "challenge_invalid":
            raise MfaChallengeInvalidError()
        if outcome == "ineligible":
            raise StaffAccessDeniedError()
        if outcome == "already_enrolled":
            raise MfaAlreadyEnrolledError()
        uri = provisioning_uri(secret=secret, account_name=account_name, issuer=policy.issuer)
        return StaffMfaEnrollment(
            issuer=policy.issuer,
            account_name=account_name,
            secret=secret,
            otpauth_uri=uri,
            qr_svg_data_url=qr_svg_data_url(uri),
        )

    def verify_mfa(
        self, raw_challenge: str | None, code: str, *, correlation_id: str
    ) -> StaffSessionIssue:
        """Complete password+TOTP sign-in and issue an MFA-verified staff session."""
        policy = self._require_mfa()
        token_hash = self._credential_codec.try_hash_presented(raw_challenge or "")
        if token_hash is None:
            raise MfaChallengeInvalidError()
        now = self._clock.now()
        access = self._credential_codec.issue()
        refresh = self._credential_codec.issue()
        outcome: str | None = None
        result: StaffSessionIssue | None = None
        try:
            with self._session_factory() as session, session.begin():
                # Match login()'s account-before-challenge order to avoid a row-lock cycle.
                challenge_user_id = self._mfa_challenge_user_id(session, token_hash)
                account = (
                    None
                    if challenge_user_id is None
                    else self._lock_mfa_account(session, challenge_user_id)
                )
                challenge = self._lock_open_challenge(session, token_hash, now)
                if challenge is None or challenge.user_id != challenge_user_id:
                    outcome = "challenge_invalid"
                elif account is None:
                    challenge.consumed_at = now
                    self._write_login_denied(
                        session, challenge.user_id, "ineligible", correlation_id
                    )
                    outcome = "ineligible"
                elif self._has_active_staff_session(session, account.id, now):
                    challenge.consumed_at = now
                    self._write_login_denied(session, account.id, "session_exists", correlation_id)
                    outcome = "conflict"
                else:
                    factor = self._factor(session, account.id, "active") or self._factor(
                        session, account.id, "pending"
                    )
                    if factor is None:
                        outcome = "enrollment_required"
                    else:
                        outcome, result = self._check_code(
                            session,
                            policy,
                            challenge=challenge,
                            account=account,
                            factor=factor,
                            code=code,
                            access=access,
                            refresh=refresh,
                            now=now,
                            correlation_id=correlation_id,
                        )
        except Exception as exc:
            raise AuditUnavailableError() from exc

        if outcome == "challenge_invalid" or outcome == "exhausted":
            raise MfaChallengeInvalidError()
        if outcome == "ineligible":
            raise StaffAccessDeniedError()
        if outcome == "conflict":
            raise StaffSessionExistsError()
        if outcome == "enrollment_required":
            raise MfaEnrollmentRequiredError()
        if outcome == "unavailable":
            raise MfaUnavailableError()
        if outcome == "code_invalid" or result is None:
            raise MfaCodeInvalidError()
        return result

    def cancel_mfa(self, raw_challenge: str | None) -> None:
        token_hash = self._credential_codec.try_hash_presented(raw_challenge or "")
        if token_hash is None:
            return
        now = self._clock.now()
        try:
            with self._session_factory() as session, session.begin():
                challenge = self._lock_open_challenge(session, token_hash, now)
                if challenge is not None:
                    challenge.consumed_at = now
        except Exception as exc:
            raise AuditUnavailableError() from exc

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
            if not self._has_portal_authority(session, account, role_name, auth_session):
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

    def refresh(self, raw_refresh: str | None, *, correlation_id: str) -> StaffSessionIssue:
        token_hash = self._credential_codec.try_hash_presented(raw_refresh or "")
        if token_hash is None:
            raise StaffSessionInvalidError()
        now = self._clock.now()
        access = self._credential_codec.issue()
        replacement = self._credential_codec.issue()
        denial = False
        replay_event: tuple[str, str] | None = None
        result: StaffSessionIssue | None = None
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
                elif not self._has_portal_authority(session, account, role_name, auth_session):
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
                    lifetimes = self._lifetimes_for(role_name)
                    absolute_expires = _utc(auth_session.absolute_expires_at)
                    idle_expires = min(now + lifetimes.idle, absolute_expires)
                    access_expires = min(now + lifetimes.access, idle_expires)
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

    def _check_code(
        self,
        session: Session,
        policy: StaffMfaPolicy,
        *,
        challenge: StaffMfaChallenge,
        account: AppUser,
        factor: AdminTotpFactor,
        code: str,
        access: IssuedCredential,
        refresh: IssuedCredential,
        now: datetime,
        correlation_id: str,
    ) -> tuple[str, StaffSessionIssue | None]:
        try:
            secret = policy.cipher.decrypt(factor.secret_ciphertext, user_id=account.id)
        except ValueError:
            return "unavailable", None
        step = policy.verifier.matching_step(secret, code, now=now)
        replayed = (
            step is not None
            and factor.last_accepted_time_step is not None
            and step <= factor.last_accepted_time_step
        )
        if step is None or replayed:
            challenge.failed_attempts += 1
            exhausted = challenge.failed_attempts >= policy.max_attempts
            if exhausted:
                challenge.consumed_at = now
            self._audit_writer.append(
                session,
                event_type="auth.mfa_failed",
                actor_id=str(account.id),
                target_id=str(account.id),
                target_type="account",
                result="failure",
                correlation_id=correlation_id,
                context={"reason": "replayed_code" if replayed else "invalid_code"},
            )
            return ("exhausted" if exhausted else "code_invalid"), None

        factor.last_accepted_time_step = step
        if factor.state == "pending":
            factor.state = "active"
            factor.activated_at = now
            self._audit_writer.append(
                session,
                event_type="auth.mfa_enrolled",
                actor_id=str(account.id),
                target_id=str(account.id),
                target_type="account",
                result="success",
                correlation_id=correlation_id,
                context={},
            )
        challenge.consumed_at = now
        role_name = str(session.scalar(select(Role.name).where(Role.id == account.role_id)))
        expiries = self._initial_expiries(role_name, now)
        self._audit_writer.append(
            session,
            event_type="auth.mfa_success",
            actor_id=str(account.id),
            target_id=str(account.id),
            target_type="account",
            result="success",
            correlation_id=correlation_id,
            context={},
        )
        session_id = self._create_browser_session(
            session,
            account.id,
            account.role_id,
            access,
            refresh,
            expiries,
            now=now,
            mfa_verified_at=now,
        )
        # SEC-02: for privileged roles a completed login means password plus MFA.
        self._audit_writer.append(
            session,
            event_type="auth.login_success",
            actor_id=str(account.id),
            target_id=str(session_id),
            target_type="staff_session",
            result="success",
            correlation_id=correlation_id,
            context={},
        )
        access_expires, idle_expires, absolute_expires = expiries
        return "verified", (
            StaffSessionUser(id=account.id, email=account.email, role=role_name),
            access.raw,
            refresh.raw,
            access_expires,
            idle_expires,
            absolute_expires,
        )

    def _lifetimes_for(self, role_name: str) -> StaffLifetimes:
        return self._admin_lifetimes if role_name == ADMIN_ROLE else STANDARD_STAFF_LIFETIMES

    def _initial_expiries(
        self, role_name: str, now: datetime
    ) -> tuple[datetime, datetime, datetime]:
        lifetimes = self._lifetimes_for(role_name)
        absolute_expires = now + lifetimes.absolute
        idle_expires = min(now + lifetimes.idle, absolute_expires)
        access_expires = min(now + lifetimes.access, idle_expires)
        return access_expires, idle_expires, absolute_expires

    def _require_mfa(self) -> StaffMfaPolicy:
        if self._mfa is None:
            raise MfaUnavailableError()
        return self._mfa

    def _issue_mfa_challenge(
        self, session: Session, user_id: int, challenge: IssuedCredential, now: datetime
    ) -> StaffMfaChallengeIssued:
        policy = self._require_mfa()
        session.execute(
            update(StaffMfaChallenge)
            .where(StaffMfaChallenge.user_id == user_id, StaffMfaChallenge.consumed_at.is_(None))
            .values(consumed_at=now)
        )
        expires_at = now + timedelta(minutes=policy.challenge_minutes)
        session.add(
            StaffMfaChallenge(
                user_id=user_id,
                token_hash=challenge.token_hash,
                created_at=now,
                expires_at=expires_at,
                failed_attempts=0,
            )
        )
        return StaffMfaChallengeIssued(
            raw_challenge=challenge.raw,
            expires_at=expires_at,
            enrollment_required=self._factor(session, user_id, "active") is None,
        )

    @staticmethod
    def _mfa_challenge_user_id(session: Session, token_hash: str) -> int | None:
        """Read challenge ownership without locking it; callers lock the account first."""
        return session.scalar(
            select(StaffMfaChallenge.user_id).where(StaffMfaChallenge.token_hash == token_hash)
        )

    @staticmethod
    def _lock_open_challenge(
        session: Session, token_hash: str, now: datetime
    ) -> StaffMfaChallenge | None:
        challenge = session.execute(
            select(StaffMfaChallenge)
            .where(StaffMfaChallenge.token_hash == token_hash)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            challenge is None
            or challenge.consumed_at is not None
            or _utc(challenge.expires_at) <= now
        ):
            return None
        return challenge

    def _lock_mfa_account(self, session: Session, user_id: int) -> AppUser | None:
        row = session.execute(
            select(AppUser, Role.name)
            .join(Role, Role.id == AppUser.role_id)
            .where(AppUser.id == user_id)
            .with_for_update(of=AppUser)
        ).one_or_none()
        if row is None:
            return None
        account: AppUser = row[0]
        role_name: str = row[1]
        if (
            not account.is_active
            or role_name not in MFA_REQUIRED_ROLES
            or not self._activation_is_authoritative(session, account, role_name)
        ):
            return None
        return account

    @staticmethod
    def _factor(session: Session, user_id: int, state: str) -> AdminTotpFactor | None:
        # The factor's joined account relationship is not needed and must not wrap the
        # LIMIT query in a subquery that FOR UPDATE could not lock.
        return session.execute(
            select(AdminTotpFactor)
            .options(lazyload(AdminTotpFactor.admin))
            .where(AdminTotpFactor.admin_user_id == user_id, AdminTotpFactor.state == state)
            .order_by(AdminTotpFactor.created_at.desc(), AdminTotpFactor.id.desc())
            .limit(1)
            .with_for_update(of=AdminTotpFactor)
        ).scalar_one_or_none()

    @staticmethod
    def _has_active_staff_session(session: Session, user_id: int, now: datetime) -> bool:
        active_ids = list(
            session.scalars(
                select(UserSession.id)
                .where(
                    UserSession.user_id == user_id,
                    UserSession.platform == "staff_browser",
                    UserSession.revoked_at.is_(None),
                    UserSession.idle_expires_at > now,
                    UserSession.absolute_expires_at > now,
                )
                .with_for_update()
            )
        )
        return bool(active_ids)

    @staticmethod
    def _create_browser_session(
        session: Session,
        user_id: int,
        role_id: int,
        access: IssuedCredential,
        refresh: IssuedCredential,
        expiries: tuple[datetime, datetime, datetime],
        *,
        now: datetime,
        mfa_verified_at: datetime | None,
    ) -> int:
        access_expires, idle_expires, absolute_expires = expiries
        browser_session = UserSession(
            user_id=user_id,
            platform="staff_browser",
            access_token_hash=access.token_hash,
            access_expires_at=access_expires,
            idle_expires_at=idle_expires,
            absolute_expires_at=absolute_expires,
            last_renewed_at=now,
            mfa_verified_at=mfa_verified_at,
            issued_role_id=role_id,
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
        return browser_session.id

    def _has_portal_authority(
        self, session: Session, account: AppUser, role_name: str, auth_session: UserSession
    ) -> bool:
        """Current active account, portal role, MFA assurance and authoritative activation."""
        if not account.is_active or role_name not in STAFF_PORTAL_ROLES:
            return False
        # A session never carries authority across a role change (ADM-04 #7-8, SEC-01 #12).
        if (
            auth_session.issued_role_id is not None
            and auth_session.issued_role_id != account.role_id
        ):
            return False
        # A session issued before a privileged role was assigned never gains MFA assurance.
        if role_name in MFA_REQUIRED_ROLES and auth_session.mfa_verified_at is None:
            return False
        # D3: an administrator additionally needs a currently active authenticator, so a
        # reset or disabled factor removes authority even from an otherwise valid session.
        if role_name == ADMIN_ROLE and self._factor(session, account.id, "active") is None:
            return False
        return self._activation_is_authoritative(session, account, role_name)

    @staticmethod
    def _activation_is_authoritative(session: Session, user: AppUser, role_name: str) -> bool:
        # Administrators are provisioned by the controlled backend procedure and never
        # through an invitation; their authority is the current role plus MFA.
        if role_name == ADMIN_ROLE:
            return True
        return bool(
            session.scalar(
                select(StaffInvitation.id)
                .join(Role, Role.id == StaffInvitation.role_id)
                .where(
                    StaffInvitation.activated_user_id == user.id,
                    StaffInvitation.consumed_at.is_not(None),
                    StaffInvitation.email == user.email,
                    StaffInvitation.role_id == user.role_id,
                    Role.name.in_(STAFF_PORTAL_ROLES),
                )
                .limit(1)
            )
        )

    def _write_login_denied(
        self, session: Session, user_id: int, reason: str, correlation_id: str
    ) -> None:
        self._audit_writer.append(
            session,
            event_type="staff.login_denied",
            actor_id=str(user_id),
            result="denied",
            correlation_id=correlation_id,
            context={"reason": reason},
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

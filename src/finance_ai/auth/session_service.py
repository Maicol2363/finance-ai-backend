from collections.abc import Callable

from finance_ai.auth.credentials import CredentialCodec, IssuedCredential
from finance_ai.auth.passwords import PasswordHasher
from finance_ai.auth.session_policy import Clock, SessionPolicy
from finance_ai.auth.session_schemas import IssuedSession, LoginCommand, PublicAuthenticatedUser
from finance_ai.auth.session_uow import SessionUnitOfWork
from finance_ai.core.errors import (
    AuthenticationRequiredError,
    InvalidCredentialsError,
    ServiceUnavailableError,
    SessionInvalidError,
)

SessionUnitOfWorkFactory = Callable[[], SessionUnitOfWork]


class SessionService:
    def __init__(
        self,
        *,
        password_hasher: PasswordHasher,
        credential_codec: CredentialCodec,
        policy: SessionPolicy,
        clock: Clock,
        uow_factory: SessionUnitOfWorkFactory,
    ) -> None:
        self.password_hasher = password_hasher
        self.credential_codec = credential_codec
        self.policy = policy
        self.clock = clock
        self.uow_factory = uow_factory

    def login(self, command: LoginCommand) -> IssuedSession:
        normalized_email = command.email.strip().lower()
        with self.uow_factory() as lookup_uow:
            snapshot = lookup_uow.users.find_auth_by_email(normalized_email)

        password_hash = snapshot.password_hash if snapshot is not None else None
        password_matches = self.password_hasher.verify_real_or_dummy(
            password_hash,
            command.password,
        )
        if snapshot is None or not snapshot.is_active or not password_matches:
            raise InvalidCredentialsError

        now = self.clock.now()
        access, renewable = self._issue_pair()
        times = self.policy.initial_times(now)

        with self.uow_factory() as uow:
            locked_user = uow.users.lock_by_id(snapshot.id)
            if (
                locked_user is None
                or not locked_user.is_active
                or locked_user.password_hash != snapshot.password_hash
            ):
                raise InvalidCredentialsError

            if self.password_hasher.needs_rehash(locked_user.password_hash):
                uow.users.update_password_hash(
                    user_id=locked_user.id,
                    password_hash=self.password_hasher.hash(command.password),
                )

            active_session_ids = uow.sessions.active_session_ids_for_user(
                user_id=locked_user.id,
                now=now,
            )
            sessions_to_revoke = max(
                0,
                len(active_session_ids) - self.policy.max_active_sessions + 1,
            )
            for session_id in active_session_ids[:sessions_to_revoke]:
                uow.sessions.revoke(session_id=session_id, now=now, reason="session_limit")

            session_id = uow.sessions.add_session(
                user_id=locked_user.id,
                platform=command.platform,
                access_token_hash=access.token_hash,
                access_expires_at=times.access_expires_at,
                idle_expires_at=times.idle_expires_at,
                absolute_expires_at=times.absolute_expires_at,
                now=now,
            )
            uow.sessions.add_renewable(
                session_id=session_id,
                token_hash=renewable.token_hash,
                issued_at=now,
                expires_at=times.idle_expires_at,
            )
            uow.commit()

        return IssuedSession(
            user=locked_user.public_user(),
            access_credential=access.raw,
            renewable_credential=renewable.raw,
            access_expires_at=times.access_expires_at,
            renewable_expires_at=times.idle_expires_at,
            absolute_expires_at=times.absolute_expires_at,
        )

    def refresh(self, raw_renewable: str) -> IssuedSession:
        renewable_hash = self.credential_codec.try_hash_presented(raw_renewable)
        if renewable_hash is None:
            raise SessionInvalidError

        now = self.clock.now()
        with self.uow_factory() as uow:
            session_id = uow.sessions.resolve_session_id_by_renewable_hash(renewable_hash)
            if session_id is None:
                raise SessionInvalidError
            session = uow.sessions.lock_session(session_id)
            renewable = uow.sessions.lock_renewable_by_hash(renewable_hash)
            if session is None or renewable is None or renewable.session_id != session_id:
                raise SessionInvalidError

            invalid_reason = self._invalid_refresh_reason(
                session_revoked=session.revoked_at is not None,
                user_active=session.user.is_active,
                renewable_consumed=renewable.consumed_at is not None,
                renewable_expired=renewable.expires_at <= now,
                idle_expired=session.idle_expires_at <= now,
                absolute_expired=session.absolute_expires_at <= now,
            )
            if invalid_reason is not None:
                if session.revoked_at is None:
                    uow.sessions.revoke(
                        session_id=session.id,
                        now=now,
                        reason=invalid_reason,
                    )
                    uow.commit()
                raise SessionInvalidError

            access, replacement = self._issue_pair()
            times = self.policy.renewed_times(
                now=now,
                absolute_expires_at=session.absolute_expires_at,
            )
            replacement_id = uow.sessions.add_renewable(
                session_id=session.id,
                token_hash=replacement.token_hash,
                issued_at=now,
                expires_at=times.idle_expires_at,
            )
            uow.sessions.rotate(
                session_id=session.id,
                previous_renewable_id=renewable.id,
                replacement_renewable_id=replacement_id,
                access_token_hash=access.token_hash,
                access_expires_at=times.access_expires_at,
                idle_expires_at=times.idle_expires_at,
                now=now,
            )
            uow.commit()

        return IssuedSession(
            user=session.user.public_user(),
            access_credential=access.raw,
            renewable_credential=replacement.raw,
            access_expires_at=times.access_expires_at,
            renewable_expires_at=times.idle_expires_at,
            absolute_expires_at=times.absolute_expires_at,
        )

    def current_user(self, raw_access: str) -> PublicAuthenticatedUser:
        access_hash = self.credential_codec.try_hash_presented(raw_access)
        if access_hash is None:
            raise AuthenticationRequiredError
        with self.uow_factory() as uow:
            principal = uow.sessions.find_principal_by_access_hash(
                access_token_hash=access_hash,
                now=self.clock.now(),
            )
        if principal is None:
            raise AuthenticationRequiredError
        return principal

    def logout(self, raw_renewable: str) -> None:
        renewable_hash = self.credential_codec.try_hash_presented(raw_renewable)
        if renewable_hash is None:
            return
        now = self.clock.now()
        with self.uow_factory() as uow:
            session_id = uow.sessions.resolve_session_id_by_renewable_hash(renewable_hash)
            if session_id is None:
                return
            session = uow.sessions.lock_session(session_id)
            if session is not None and session.revoked_at is None:
                uow.sessions.revoke(session_id=session_id, now=now, reason="logout")
            uow.commit()

    def cleanup_terminal_sessions(self, *, batch_size: int = 100) -> int:
        if batch_size < 1 or batch_size > 1_000:
            raise ValueError("Cleanup batch size must be between 1 and 1000")
        now = self.clock.now()
        with self.uow_factory() as uow:
            deleted = uow.sessions.cleanup_terminal(
                cutoff=self.policy.cleanup_cutoff(now),
                now=now,
                batch_size=batch_size,
            )
            uow.commit()
            return deleted

    def ensure_ready(self) -> None:
        self.password_hasher.ensure_ready()
        try:
            first, second = self._issue_pair()
        except ServiceUnavailableError:
            raise
        if first.raw == second.raw or first.token_hash == second.token_hash:
            raise ServiceUnavailableError

    def _issue_pair(self) -> tuple[IssuedCredential, IssuedCredential]:
        try:
            access = self.credential_codec.issue()
            renewable = self.credential_codec.issue()
        except Exception as exc:
            raise ServiceUnavailableError from exc
        if access.raw == renewable.raw or access.token_hash == renewable.token_hash:
            raise ServiceUnavailableError
        return access, renewable

    @staticmethod
    def _invalid_refresh_reason(
        *,
        session_revoked: bool,
        user_active: bool,
        renewable_consumed: bool,
        renewable_expired: bool,
        idle_expired: bool,
        absolute_expired: bool,
    ) -> str | None:
        if session_revoked:
            return "revoked"
        if not user_active:
            return "user_inactive"
        if renewable_consumed:
            return "replay"
        if absolute_expired:
            return "absolute_expired"
        if idle_expired or renewable_expired:
            return "idle_expired"
        return None

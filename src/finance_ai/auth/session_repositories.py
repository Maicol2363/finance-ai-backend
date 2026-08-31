from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from finance_ai.auth.session_schemas import PublicAuthenticatedUser


@dataclass(frozen=True)
class AuthUserRecord:
    id: int
    email: str
    password_hash: str
    preferred_currency: str
    is_active: bool

    def public_user(self) -> PublicAuthenticatedUser:
        return PublicAuthenticatedUser(
            user_id=self.id,
            email=self.email,
            preferred_currency=self.preferred_currency,
        )


@dataclass(frozen=True)
class SessionRecord:
    id: int
    user: AuthUserRecord
    access_expires_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class RenewableRecord:
    id: int
    session_id: int
    expires_at: datetime
    consumed_at: datetime | None


class SessionUserRepository(Protocol):
    def find_auth_by_email(self, normalized_email: str) -> AuthUserRecord | None: ...

    def lock_by_id(self, user_id: int) -> AuthUserRecord | None: ...

    def update_password_hash(self, *, user_id: int, password_hash: str) -> None: ...


class SessionRepository(Protocol):
    def active_session_ids_for_user(self, *, user_id: int, now: datetime) -> list[int]: ...

    def add_session(
        self,
        *,
        user_id: int,
        platform: str,
        access_token_hash: str,
        access_expires_at: datetime,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
        now: datetime,
    ) -> int: ...

    def add_renewable(
        self,
        *,
        session_id: int,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> int: ...

    def resolve_session_id_by_renewable_hash(self, token_hash: str) -> int | None: ...

    def lock_session(self, session_id: int) -> SessionRecord | None: ...

    def lock_renewable_by_hash(self, token_hash: str) -> RenewableRecord | None: ...

    def rotate(
        self,
        *,
        session_id: int,
        previous_renewable_id: int,
        replacement_renewable_id: int,
        access_token_hash: str,
        access_expires_at: datetime,
        idle_expires_at: datetime,
        now: datetime,
    ) -> None: ...

    def revoke(self, *, session_id: int, now: datetime, reason: str) -> None: ...

    def find_principal_by_access_hash(
        self, *, access_token_hash: str, now: datetime
    ) -> PublicAuthenticatedUser | None: ...

    def cleanup_terminal(self, *, cutoff: datetime, now: datetime, batch_size: int) -> int: ...

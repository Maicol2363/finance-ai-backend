from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.session_policy import SessionPolicy
from finance_ai.auth.session_repositories import (
    AuthUserRecord,
    RenewableRecord,
    SessionRecord,
)
from finance_ai.auth.session_schemas import LoginCommand, PublicAuthenticatedUser
from finance_ai.auth.session_service import SessionService
from finance_ai.core.errors import (
    AuthenticationRequiredError,
    InvalidCredentialsError,
    SessionInvalidError,
)


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class SequentialBytes:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, length: int) -> bytes:
        self.value += 1
        return bytes([self.value]) * length


class FakePasswordHasher:
    def __init__(self) -> None:
        self.rehash = False
        self.verified_hashes: list[str | None] = []

    def verify_real_or_dummy(self, password_hash: str | None, candidate: str) -> bool:
        self.verified_hashes.append(password_hash)
        return password_hash is not None and candidate == "correct-password"

    def needs_rehash(self, password_hash: str) -> bool:
        return self.rehash

    def hash(self, password: str) -> str:
        assert password == "correct-password"
        return "rehash-value"

    def ensure_ready(self) -> None:
        return None


@dataclass
class MutableSession:
    id: int
    user_id: int
    platform: str
    access_token_hash: str
    access_expires_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    created_at: datetime
    last_renewed_at: datetime
    revoked_at: datetime | None = None
    revocation_reason: str | None = None


@dataclass
class MutableRenewable:
    id: int
    session_id: int
    token_hash: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    replaced_by_id: int | None = None


@dataclass
class Store:
    user: AuthUserRecord | None
    sessions: dict[int, MutableSession] = field(default_factory=dict)
    renewables: dict[int, MutableRenewable] = field(default_factory=dict)
    next_session_id: int = 1
    next_renewable_id: int = 1
    commits: int = 0


class FakeUsers:
    def __init__(self, store: Store) -> None:
        self.store = store

    def find_auth_by_email(self, normalized_email: str) -> AuthUserRecord | None:
        if self.store.user is None or self.store.user.email != normalized_email:
            return None
        return self.store.user

    def lock_by_id(self, user_id: int) -> AuthUserRecord | None:
        if self.store.user is None or self.store.user.id != user_id:
            return None
        return self.store.user

    def update_password_hash(self, *, user_id: int, password_hash: str) -> None:
        assert self.store.user is not None and self.store.user.id == user_id
        self.store.user = AuthUserRecord(
            id=self.store.user.id,
            email=self.store.user.email,
            password_hash=password_hash,
            preferred_currency=self.store.user.preferred_currency,
            is_active=self.store.user.is_active,
        )


class FakeSessions:
    def __init__(self, store: Store) -> None:
        self.store = store

    def active_session_ids_for_user(self, *, user_id: int, now: datetime) -> list[int]:
        return [
            item.id
            for item in sorted(self.store.sessions.values(), key=lambda value: value.id)
            if item.user_id == user_id
            and item.revoked_at is None
            and item.idle_expires_at > now
            and item.absolute_expires_at > now
        ]

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
    ) -> int:
        session_id = self.store.next_session_id
        self.store.next_session_id += 1
        self.store.sessions[session_id] = MutableSession(
            id=session_id,
            user_id=user_id,
            platform=platform,
            access_token_hash=access_token_hash,
            access_expires_at=access_expires_at,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            created_at=now,
            last_renewed_at=now,
        )
        return session_id

    def add_renewable(
        self,
        *,
        session_id: int,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> int:
        renewable_id = self.store.next_renewable_id
        self.store.next_renewable_id += 1
        self.store.renewables[renewable_id] = MutableRenewable(
            id=renewable_id,
            session_id=session_id,
            token_hash=token_hash,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        return renewable_id

    def resolve_session_id_by_renewable_hash(self, token_hash: str) -> int | None:
        item = next(
            (value for value in self.store.renewables.values() if value.token_hash == token_hash),
            None,
        )
        return item.session_id if item is not None else None

    def lock_session(self, session_id: int) -> SessionRecord | None:
        item = self.store.sessions.get(session_id)
        if item is None or self.store.user is None:
            return None
        return SessionRecord(
            id=item.id,
            user=self.store.user,
            access_expires_at=item.access_expires_at,
            idle_expires_at=item.idle_expires_at,
            absolute_expires_at=item.absolute_expires_at,
            revoked_at=item.revoked_at,
        )

    def lock_renewable_by_hash(self, token_hash: str) -> RenewableRecord | None:
        item = next(
            (value for value in self.store.renewables.values() if value.token_hash == token_hash),
            None,
        )
        if item is None:
            return None
        return RenewableRecord(
            id=item.id,
            session_id=item.session_id,
            expires_at=item.expires_at,
            consumed_at=item.consumed_at,
        )

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
    ) -> None:
        previous = self.store.renewables[previous_renewable_id]
        previous.consumed_at = now
        previous.replaced_by_id = replacement_renewable_id
        session = self.store.sessions[session_id]
        session.access_token_hash = access_token_hash
        session.access_expires_at = access_expires_at
        session.idle_expires_at = idle_expires_at
        session.last_renewed_at = now

    def revoke(self, *, session_id: int, now: datetime, reason: str) -> None:
        session = self.store.sessions[session_id]
        if session.revoked_at is None:
            session.revoked_at = now
            session.revocation_reason = reason

    def find_principal_by_access_hash(
        self, *, access_token_hash: str, now: datetime
    ) -> PublicAuthenticatedUser | None:
        item = next(
            (
                value
                for value in self.store.sessions.values()
                if value.access_token_hash == access_token_hash
                and value.revoked_at is None
                and value.access_expires_at > now
                and value.idle_expires_at > now
                and value.absolute_expires_at > now
            ),
            None,
        )
        if item is None or self.store.user is None or not self.store.user.is_active:
            return None
        return self.store.user.public_user()

    def cleanup_terminal(self, *, cutoff: datetime, now: datetime, batch_size: int) -> int:
        eligible = [
            item.id
            for item in self.store.sessions.values()
            if (
                (item.revoked_at is not None and item.revoked_at < cutoff)
                or item.absolute_expires_at < cutoff
            )
            and (item.revoked_at is not None or item.absolute_expires_at <= now)
        ][:batch_size]
        for session_id in eligible:
            self.store.sessions.pop(session_id)
            self.store.renewables = {
                key: value
                for key, value in self.store.renewables.items()
                if value.session_id != session_id
            }
        return len(eligible)


class FakeUow:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.users = FakeUsers(store)
        self.sessions = FakeSessions(store)

    def __enter__(self) -> FakeUow:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.store.commits += 1

    def rollback(self) -> None:
        return None


def make_service() -> tuple[SessionService, Store, FrozenClock, FakePasswordHasher]:
    user = AuthUserRecord(
        id=7,
        email="person@example.com",
        password_hash="stored-hash",
        preferred_currency="COP",
        is_active=True,
    )
    store = Store(user=user)
    clock = FrozenClock(datetime(2026, 8, 30, tzinfo=UTC))
    hasher = FakePasswordHasher()
    service = SessionService(
        password_hasher=hasher,  # type: ignore[arg-type]
        credential_codec=CredentialCodec(SequentialBytes()),
        policy=SessionPolicy(
            access_lifetime=timedelta(minutes=15),
            renewable_idle_lifetime=timedelta(days=30),
            absolute_lifetime=timedelta(days=90),
            max_active_sessions=10,
            terminal_retention=timedelta(days=30),
        ),
        clock=clock,
        uow_factory=lambda: FakeUow(store),  # type: ignore[arg-type]
    )
    return service, store, clock, hasher


def login(service: SessionService) -> object:
    return service.login(
        LoginCommand(
            email=" Person@Example.COM ",
            password="correct-password",
            platform="windows",
        )
    )


def test_login_persists_only_hashes_and_access_resolves_current_user() -> None:
    service, store, _, _ = make_service()
    issued = login(service)

    assert issued.user.email == "person@example.com"
    session = next(iter(store.sessions.values()))
    renewable = next(iter(store.renewables.values()))
    assert session.access_token_hash != issued.access_credential
    assert renewable.token_hash != issued.renewable_credential
    assert service.current_user(issued.access_credential) == issued.user


def test_unknown_and_wrong_passwords_share_the_generic_failure_and_dummy_path() -> None:
    service, store, _, hasher = make_service()
    store.user = None
    with pytest.raises(InvalidCredentialsError) as unknown:
        login(service)
    assert unknown.value.code == "invalid_credentials"
    assert hasher.verified_hashes == [None]

    service, store, _, hasher = make_service()
    with pytest.raises(InvalidCredentialsError) as wrong:
        service.login(
            LoginCommand(
                email="person@example.com",
                password="wrong-password",
                platform="android",
            )
        )
    assert wrong.value.detail == unknown.value.detail
    assert hasher.verified_hashes == ["stored-hash"]
    assert store.sessions == {}


def test_login_rehashes_and_deterministically_limits_active_sessions() -> None:
    service, store, _, hasher = make_service()
    hasher.rehash = True
    issued_values = [login(service) for _ in range(11)]
    active = [item for item in store.sessions.values() if item.revoked_at is None]

    assert store.user is not None and store.user.password_hash == "rehash-value"
    assert len(active) == 10
    assert store.sessions[1].revocation_reason == "session_limit"
    assert issued_values[-1].access_credential


def test_refresh_rotates_both_credentials_and_replay_revokes_the_family() -> None:
    service, store, _, _ = make_service()
    initial = login(service)
    rotated = service.refresh(initial.renewable_credential)

    assert rotated.access_credential != initial.access_credential
    assert rotated.renewable_credential != initial.renewable_credential
    with pytest.raises(AuthenticationRequiredError):
        service.current_user(initial.access_credential)
    assert service.current_user(rotated.access_credential) == rotated.user

    with pytest.raises(SessionInvalidError):
        service.refresh(initial.renewable_credential)
    assert store.sessions[1].revocation_reason == "replay"
    with pytest.raises(AuthenticationRequiredError):
        service.current_user(rotated.access_credential)


def test_expiry_logout_and_cleanup_fail_closed_and_are_idempotent() -> None:
    service, store, clock, _ = make_service()
    issued = login(service)
    clock.value += timedelta(days=31)
    with pytest.raises(SessionInvalidError):
        service.refresh(issued.renewable_credential)
    assert store.sessions[1].revocation_reason == "idle_expired"

    service.logout("not-a-credential")
    service.logout(issued.renewable_credential)
    service.logout(issued.renewable_credential)
    clock.value += timedelta(days=31)
    assert service.cleanup_terminal_sessions(batch_size=1) == 1
    assert service.cleanup_terminal_sessions(batch_size=1) == 0
    with pytest.raises(ValueError):
        service.cleanup_terminal_sessions(batch_size=0)

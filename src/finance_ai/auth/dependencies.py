from datetime import timedelta
from functools import lru_cache

from fastapi import Header, Request

from finance_ai.auth.credentials import CredentialCodec
from finance_ai.auth.passwords import PasswordHasher, UnsafePasswordProvider
from finance_ai.auth.policy import RegistrationPolicy
from finance_ai.auth.service import RegistrationService
from finance_ai.auth.session_policy import SessionPolicy, SystemClock
from finance_ai.auth.session_service import SessionService
from finance_ai.auth.session_uow import SessionUnitOfWork
from finance_ai.auth.uow import RegistrationUnitOfWork
from finance_ai.core.config import get_settings
from finance_ai.core.errors import AuthenticationRequiredError
from finance_ai.core.rate_limit import (
    AuthRateLimiter,
    get_auth_rate_limiter,
    get_registration_rate_limiter,
)
from finance_ai.db.session import get_session_factory
from finance_ai.db.session_uow import SQLAlchemySessionUnitOfWork
from finance_ai.db.uow import SQLAlchemyRegistrationUnitOfWork


@lru_cache
def get_unsafe_password_provider() -> UnsafePasswordProvider:
    return UnsafePasswordProvider(get_settings().resolved_unsafe_passwords_path)


@lru_cache
def get_password_hasher() -> PasswordHasher:
    settings = get_settings()
    return PasswordHasher(
        memory_kib=settings.argon2_memory_kib,
        time_cost=settings.argon2_time_cost,
        parallelism=settings.argon2_parallelism,
    )


def get_registration_service() -> RegistrationService:
    def uow_factory() -> RegistrationUnitOfWork:
        return SQLAlchemyRegistrationUnitOfWork(get_session_factory())

    return RegistrationService(
        policy=RegistrationPolicy(),
        unsafe_passwords=get_unsafe_password_provider(),
        password_hasher=get_password_hasher(),
        uow_factory=uow_factory,
    )


def enforce_registration_rate_limit(request: Request) -> None:
    client_key = request.client.host if request.client is not None else "unknown"
    get_registration_rate_limiter().check(client_key)


@lru_cache
def get_credential_codec() -> CredentialCodec:
    return CredentialCodec()


@lru_cache
def get_session_policy() -> SessionPolicy:
    settings = get_settings()
    return SessionPolicy(
        access_lifetime=timedelta(minutes=settings.access_lifetime_minutes),
        renewable_idle_lifetime=timedelta(days=settings.renewable_idle_lifetime_days),
        absolute_lifetime=timedelta(days=settings.session_absolute_lifetime_days),
        max_active_sessions=settings.max_active_sessions_per_user,
        terminal_retention=timedelta(days=settings.terminal_session_retention_days),
    )


def get_session_service() -> SessionService:
    def uow_factory() -> SessionUnitOfWork:
        return SQLAlchemySessionUnitOfWork(get_session_factory())

    return SessionService(
        password_hasher=get_password_hasher(),
        credential_codec=get_credential_codec(),
        policy=get_session_policy(),
        clock=SystemClock(),
        uow_factory=uow_factory,
    )


def get_session_rate_limiter() -> AuthRateLimiter:
    return get_auth_rate_limiter()


def require_bearer_access(authorization: str | None = Header(default=None)) -> str:
    if authorization is None:
        raise AuthenticationRequiredError
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].casefold() != "bearer" or not parts[1]:
        raise AuthenticationRequiredError
    return parts[1]

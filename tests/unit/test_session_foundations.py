from datetime import UTC, datetime, timedelta

import pytest
from limits.storage import MemoryStorage

from finance_ai.auth.session_policy import SessionPolicy
from finance_ai.core.config import Settings
from finance_ai.core.errors import LoginRateLimitedError
from finance_ai.core.rate_limit import AccountKeyHasher, AuthRateLimiter


def policy() -> SessionPolicy:
    return SessionPolicy(
        access_lifetime=timedelta(minutes=15),
        renewable_idle_lifetime=timedelta(days=30),
        absolute_lifetime=timedelta(days=90),
        max_active_sessions=10,
        terminal_retention=timedelta(days=30),
    )


def test_session_policy_uses_short_access_and_bounded_renewal() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    initial = policy().initial_times(now)
    renewed = policy().renewed_times(
        now=now + timedelta(days=89, hours=23, minutes=55),
        absolute_expires_at=initial.absolute_expires_at,
    )

    assert initial.access_expires_at == now + timedelta(minutes=15)
    assert initial.idle_expires_at == now + timedelta(days=30)
    assert initial.absolute_expires_at == now + timedelta(days=90)
    assert renewed.idle_expires_at == initial.absolute_expires_at
    assert renewed.access_expires_at == initial.absolute_expires_at
    assert policy().cleanup_cutoff(now) == now - timedelta(days=30)


def test_settings_reject_insecure_or_inconsistent_production_values() -> None:
    with pytest.raises(ValueError):
        Settings(environment="production")
    with pytest.raises(ValueError):
        Settings(renewable_idle_lifetime_days=91, session_absolute_lifetime_days=90)


def test_account_limiter_key_is_stable_secret_keyed_and_not_the_email() -> None:
    first = AccountKeyHasher("a" * 32).key_for("person@example.com")
    second = AccountKeyHasher("b" * 32).key_for("person@example.com")
    assert len(first) == 64
    assert first != second
    assert "person" not in first


def test_auth_rate_limiter_isolates_login_and_refresh_namespaces() -> None:
    limiter = AuthRateLimiter(
        login_per_minute=2,
        login_burst_per_10_seconds=2,
        login_account_per_minute=2,
        login_global_per_minute=10,
        refresh_per_minute=1,
        refresh_global_per_minute=10,
        account_key_hasher=AccountKeyHasher("a" * 32),
        storage=MemoryStorage(),
    )
    limiter.check_login(client_key="client", normalized_email="person@example.com")
    limiter.check_login(client_key="client", normalized_email="person@example.com")
    with pytest.raises(LoginRateLimitedError) as login_failure:
        limiter.check_login(client_key="client", normalized_email="person@example.com")
    assert int(login_failure.value.headers["Retry-After"]) >= 1

    limiter.check_refresh(client_key="client")
    with pytest.raises(LoginRateLimitedError):
        limiter.check_refresh(client_key="client")
    limiter.reset()
    limiter.check_refresh(client_key="client")

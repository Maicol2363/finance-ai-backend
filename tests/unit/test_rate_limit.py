import pytest

from finance_ai.core.errors import RegistrationRateLimitedError
from finance_ai.core.rate_limit import RegistrationRateLimiter


def test_client_burst_limit_rejects_and_reset_restores_capacity() -> None:
    limiter = RegistrationRateLimiter(
        per_minute=10,
        burst_per_10_seconds=1,
        global_per_minute=100,
    )

    limiter.check("client-a")
    with pytest.raises(RegistrationRateLimitedError) as captured:
        limiter.check("client-a")

    assert captured.value.status == 429
    assert int((captured.value.headers or {})["Retry-After"]) >= 1
    limiter.reset()
    limiter.check("client-a")


def test_global_limit_applies_across_client_keys() -> None:
    limiter = RegistrationRateLimiter(
        per_minute=10,
        burst_per_10_seconds=10,
        global_per_minute=1,
    )

    limiter.check("client-a")
    with pytest.raises(RegistrationRateLimitedError):
        limiter.check("client-b")

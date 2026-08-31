import hashlib
import hmac
import math
import time
from dataclasses import dataclass
from functools import lru_cache

from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter

from finance_ai.core.config import get_settings
from finance_ai.core.errors import LoginRateLimitedError, RegistrationRateLimitedError


@dataclass(frozen=True)
class RateLimitRule:
    item: RateLimitItem
    namespace: str


class RegistrationRateLimiter:
    def __init__(
        self,
        *,
        per_minute: int,
        burst_per_10_seconds: int,
        global_per_minute: int,
        storage: MemoryStorage | None = None,
    ) -> None:
        self.storage = storage or MemoryStorage()
        self.strategy = FixedWindowRateLimiter(self.storage)
        self.client_rules = (
            RateLimitRule(parse(f"{burst_per_10_seconds}/10 seconds"), "registration-burst"),
            RateLimitRule(parse(f"{per_minute}/minute"), "registration-client"),
        )
        self.global_rule = RateLimitRule(
            parse(f"{global_per_minute}/minute"), "registration-global"
        )

    def check(self, client_key: str) -> None:
        for rule in self.client_rules:
            self._hit_or_raise(rule, client_key)
        self._hit_or_raise(self.global_rule, "all")

    def reset(self) -> None:
        self.storage.reset()

    def _hit_or_raise(self, rule: RateLimitRule, key: str) -> None:
        if self.strategy.hit(rule.item, rule.namespace, key):
            return
        stats = self.strategy.get_window_stats(rule.item, rule.namespace, key)
        retry_after = max(1, math.ceil(stats.reset_time - time.time()))
        raise RegistrationRateLimitedError(retry_after)


@lru_cache
def get_registration_rate_limiter() -> RegistrationRateLimiter:
    settings = get_settings()
    return RegistrationRateLimiter(
        per_minute=settings.registration_per_minute,
        burst_per_10_seconds=settings.registration_burst_per_10_seconds,
        global_per_minute=settings.registration_global_per_minute,
    )


class AccountKeyHasher:
    """Creates stable process-safe limiter keys without retaining raw account identifiers."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")

    def key_for(self, normalized_email: str) -> str:
        return hmac.new(
            self._secret,
            normalized_email.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


class AuthRateLimiter:
    def __init__(
        self,
        *,
        login_per_minute: int,
        login_burst_per_10_seconds: int,
        login_account_per_minute: int,
        login_global_per_minute: int,
        refresh_per_minute: int,
        refresh_global_per_minute: int,
        account_key_hasher: AccountKeyHasher,
        storage: MemoryStorage | None = None,
    ) -> None:
        self.storage = storage or MemoryStorage()
        self.strategy = FixedWindowRateLimiter(self.storage)
        self.account_key_hasher = account_key_hasher
        self.login_client_rules = (
            RateLimitRule(parse(f"{login_burst_per_10_seconds}/10 seconds"), "login-burst"),
            RateLimitRule(parse(f"{login_per_minute}/minute"), "login-client"),
        )
        self.login_account_rule = RateLimitRule(
            parse(f"{login_account_per_minute}/minute"), "login-account"
        )
        self.login_global_rule = RateLimitRule(
            parse(f"{login_global_per_minute}/minute"), "login-global"
        )
        self.refresh_client_rule = RateLimitRule(
            parse(f"{refresh_per_minute}/minute"), "refresh-client"
        )
        self.refresh_global_rule = RateLimitRule(
            parse(f"{refresh_global_per_minute}/minute"), "refresh-global"
        )

    def check_login(self, *, client_key: str, normalized_email: str) -> None:
        account_key = self.account_key_hasher.key_for(normalized_email)
        for rule in self.login_client_rules:
            self._hit_or_raise(rule, client_key)
        self._hit_or_raise(self.login_account_rule, account_key)
        self._hit_or_raise(self.login_global_rule, "all")

    def check_refresh(self, *, client_key: str) -> None:
        self._hit_or_raise(self.refresh_client_rule, client_key)
        self._hit_or_raise(self.refresh_global_rule, "all")

    def reset(self) -> None:
        self.storage.reset()

    def _hit_or_raise(self, rule: RateLimitRule, key: str) -> None:
        if self.strategy.hit(rule.item, rule.namespace, key):
            return
        stats = self.strategy.get_window_stats(rule.item, rule.namespace, key)
        retry_after = max(1, math.ceil(stats.reset_time - time.time()))
        raise LoginRateLimitedError(retry_after)


@lru_cache
def get_auth_rate_limiter() -> AuthRateLimiter:
    settings = get_settings()
    return AuthRateLimiter(
        login_per_minute=settings.login_per_minute,
        login_burst_per_10_seconds=settings.login_burst_per_10_seconds,
        login_account_per_minute=settings.login_account_per_minute,
        login_global_per_minute=settings.login_global_per_minute,
        refresh_per_minute=settings.refresh_per_minute,
        refresh_global_per_minute=settings.refresh_global_per_minute,
        account_key_hasher=AccountKeyHasher(settings.session_limiter_secret),
    )

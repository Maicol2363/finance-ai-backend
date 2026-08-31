from pathlib import Path

from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.low_level import Type

from finance_ai.core.errors import RegistrationValidationError, ServiceUnavailableError


class UnsafePasswordProvider:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._passwords: frozenset[str] | None = None

    def ensure_ready(self) -> None:
        self._load()

    def reject_if_unsafe(self, candidate: str) -> None:
        passwords = self._load()
        if candidate.casefold() in passwords:
            raise RegistrationValidationError(
                {"password": ["Choose a password that is not commonly compromised."]}
            )

    def _load(self) -> frozenset[str]:
        if self._passwords is not None:
            return self._passwords
        try:
            values = {
                line.strip().casefold()
                for line in self.path.read_text(encoding="utf-8", errors="strict").splitlines()
                if line.strip() and not line.startswith("#")
            }
        except (OSError, UnicodeError) as exc:
            raise ServiceUnavailableError from exc
        if not values:
            raise ServiceUnavailableError
        self._passwords = frozenset(values)
        return self._passwords


class PasswordHasher:
    def __init__(self, *, memory_kib: int, time_cost: int, parallelism: int) -> None:
        self._hasher = Argon2PasswordHasher(
            memory_cost=memory_kib,
            time_cost=time_cost,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash = self.hash("finance-ai-dummy-password-not-a-user")

    def hash(self, password: str) -> str:
        try:
            return self._hasher.hash(password)
        except Exception as exc:
            raise ServiceUnavailableError from exc

    def verify(self, password_hash: str, candidate: str) -> bool:
        try:
            return self._hasher.verify(password_hash, candidate)
        except Exception:
            return False

    def verify_real_or_dummy(self, password_hash: str | None, candidate: str) -> bool:
        return self.verify(password_hash or self._dummy_hash, candidate)

    def ensure_ready(self) -> None:
        if not self.verify(self._dummy_hash, "finance-ai-dummy-password-not-a-user"):
            raise ServiceUnavailableError

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)

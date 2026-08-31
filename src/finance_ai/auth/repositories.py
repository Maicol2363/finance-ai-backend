from dataclasses import dataclass
from typing import Protocol

from finance_ai.db.seeds import RegistrationCatalogTemplate


@dataclass(frozen=True)
class CurrencyRecord:
    id: int
    code: str


@dataclass(frozen=True)
class UserRecord:
    id: int
    email: str


class CurrencyRepository(Protocol):
    def require_active_by_code(self, code: str) -> CurrencyRecord: ...


class UserRepository(Protocol):
    def exists_by_email(self, normalized_email: str) -> bool: ...

    def add(self, *, email: str, password_hash: str, preferred_currency_id: int) -> UserRecord: ...


class CatalogRepository(Protocol):
    def add_defaults(self, *, user_id: int, template: RegistrationCatalogTemplate) -> None: ...

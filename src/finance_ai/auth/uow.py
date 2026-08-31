from types import TracebackType
from typing import Protocol, Self

from finance_ai.auth.repositories import CatalogRepository, CurrencyRepository, UserRepository


class RegistrationUnitOfWork(Protocol):
    currencies: CurrencyRepository
    users: UserRepository
    catalogs: CatalogRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def flush(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

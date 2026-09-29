from types import TracebackType
from typing import Protocol, Self

from finance_ai.accounts.repositories import CurrencyRepository
from finance_ai.budgets.repositories import BudgetRepository


class BudgetUnitOfWork(Protocol):
    budgets: BudgetRepository
    currencies: CurrencyRepository

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

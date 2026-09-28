"""Transaction boundary contract used by the expense service."""

from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self

from finance_ai.expenses.repositories import ExpenseRepository

if TYPE_CHECKING:
    from finance_ai.categories.repositories import CategoryRepository
    from finance_ai.receipts.repository import ReceiptProposalRepository


class ExpenseUnitOfWork(Protocol):
    expenses: ExpenseRepository
    proposals: ReceiptProposalRepository
    categories: CategoryRepository

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

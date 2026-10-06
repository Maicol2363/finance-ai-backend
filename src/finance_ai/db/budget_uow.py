from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from finance_ai.accounts.repositories import CurrencyRepository
from finance_ai.budgets.repositories import BudgetRepository
from finance_ai.db.repositories.accounts import SQLAlchemyAccountCurrencyRepository
from finance_ai.db.repositories.budgets import SQLAlchemyBudgetRepository


class SQLAlchemyBudgetUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.session: Session | None = None
        self.budgets: BudgetRepository
        self.currencies: CurrencyRepository
        self._committed = False

    def __enter__(self) -> Self:
        self.session = self.session_factory()
        self.budgets = SQLAlchemyBudgetRepository(self.session)
        self.currencies = SQLAlchemyAccountCurrencyRepository(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        if self.session is None:
            return
        try:
            if exc_type is not None or not self._committed:
                self.session.rollback()
        finally:
            self.session.close()

    def flush(self) -> None:
        self._require_session().flush()

    def commit(self) -> None:
        self._require_session().commit()
        self._committed = True

    def rollback(self) -> None:
        self._require_session().rollback()

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("Unit of Work has not been entered")
        return self.session

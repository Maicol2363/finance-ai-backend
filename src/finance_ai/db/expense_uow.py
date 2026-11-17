"""SQLAlchemy transaction boundary for HU-008 and HU-011 persistence."""

from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from finance_ai.categories.repositories import CategoryRepository
from finance_ai.core.logging import get_logger
from finance_ai.db.repositories.categories import SQLAlchemyCategoryRepository
from finance_ai.db.repositories.expenses import SQLAlchemyExpenseRepository
from finance_ai.expenses.repositories import ExpenseRepository
from finance_ai.notifications.budget_alerts import BudgetAlertEvaluator
from finance_ai.receipts.repository import ReceiptProposalRepository

logger = get_logger(__name__)


class SQLAlchemyExpenseUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.session: Session | None = None
        self.expenses: ExpenseRepository
        self.proposals: ReceiptProposalRepository
        self.categories: CategoryRepository
        self._committed = False

    def __enter__(self) -> Self:
        self.session = self.session_factory()
        self.expenses = SQLAlchemyExpenseRepository(self.session)
        self.proposals = ReceiptProposalRepository(self.session)
        self.categories = SQLAlchemyCategoryRepository(self.session)
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

    def evaluate_budget_alerts(self, *, owner_id: int) -> None:
        """Runs in a savepoint: a problem while deciding about an email is logged and undone, and
        the person's expense is still saved."""
        session = self._require_session()
        try:
            with session.begin_nested():
                BudgetAlertEvaluator(session).evaluate(owner_id)
        except Exception as error:
            logger.warning(
                "Budget alert evaluation failed",
                extra={
                    "event": "budget_alert.evaluate",
                    "outcome": "failed",
                    "error_type": type(error).__name__,
                },
            )

    def commit(self) -> None:
        self._require_session().commit()
        self._committed = True

    def rollback(self) -> None:
        self._require_session().rollback()

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("Unit of Work has not been entered")
        return self.session

"""FastAPI dependency factories for the expense application service."""

from finance_ai.db.expense_uow import SQLAlchemyExpenseUnitOfWork
from finance_ai.db.session import get_session_factory
from finance_ai.expenses.service import ExpenseService


def get_expense_service() -> ExpenseService:
    return ExpenseService(uow_factory=lambda: SQLAlchemyExpenseUnitOfWork(get_session_factory()))

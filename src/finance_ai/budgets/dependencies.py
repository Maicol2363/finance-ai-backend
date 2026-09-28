from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends

from finance_ai.auth.dependencies import get_session_service, require_bearer_access
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.auth.session_service import SessionService
from finance_ai.budgets.policy import BudgetPolicy
from finance_ai.budgets.service import BudgetService
from finance_ai.budgets.uow import BudgetUnitOfWork
from finance_ai.db.budget_uow import SQLAlchemyBudgetUnitOfWork
from finance_ai.db.session import get_session_factory


def get_budget_service() -> BudgetService:
    def uow_factory() -> BudgetUnitOfWork:
        return SQLAlchemyBudgetUnitOfWork(get_session_factory())

    return BudgetService(
        policy=BudgetPolicy(),
        uow_factory=uow_factory,
        today_factory=lambda: datetime.now(UTC).date(),
    )


def get_authenticated_principal(
    access_credential: Annotated[str, Depends(require_bearer_access)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> PublicAuthenticatedUser:
    return session_service.current_user(access_credential)

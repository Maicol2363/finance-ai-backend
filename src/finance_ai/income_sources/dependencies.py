from typing import Annotated

from fastapi import Depends

from finance_ai.auth.dependencies import get_session_service, require_bearer_access
from finance_ai.auth.session_service import SessionService
from finance_ai.db.session import get_session_factory
from finance_ai.db.uow import SQLAlchemyIncomeSourceUnitOfWork
from finance_ai.income_sources.service import IncomeSourceService
from finance_ai.income_sources.uow import IncomeSourceUnitOfWork


def get_current_user_id(
    authorization: Annotated[str, Depends(require_bearer_access)],
    service: Annotated[SessionService, Depends(get_session_service)],
) -> int:
    return service.current_user(authorization).user_id


def get_income_source_service() -> IncomeSourceService:
    def uow_factory() -> IncomeSourceUnitOfWork:
        return SQLAlchemyIncomeSourceUnitOfWork(get_session_factory())

    return IncomeSourceService(uow_factory=uow_factory)

from typing import Annotated

from fastapi import Depends

from finance_ai.auth.dependencies import get_session_service, require_bearer_access
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.auth.session_service import SessionService
from finance_ai.categories.policy import CategoryPolicy
from finance_ai.categories.service import CategoryService
from finance_ai.categories.uow import CategoryUnitOfWork
from finance_ai.db.category_uow import SQLAlchemyCategoryUnitOfWork
from finance_ai.db.session import get_session_factory


def get_category_service() -> CategoryService:
    def uow_factory() -> CategoryUnitOfWork:
        return SQLAlchemyCategoryUnitOfWork(get_session_factory())

    return CategoryService(policy=CategoryPolicy(), uow_factory=uow_factory)


def get_authenticated_principal(
    access_credential: Annotated[str, Depends(require_bearer_access)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> PublicAuthenticatedUser:
    return session_service.current_user(access_credential)

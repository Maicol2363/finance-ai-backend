from typing import Annotated

from fastapi import Depends

from finance_ai.accounts.policy import AccountPolicy
from finance_ai.accounts.service import AccountService
from finance_ai.accounts.uow import AccountUnitOfWork
from finance_ai.auth.dependencies import (
    get_session_service,
    require_bearer_access,
)
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.auth.session_service import SessionService
from finance_ai.db.account_uow import SQLAlchemyAccountUnitOfWork
from finance_ai.db.session import get_session_factory


def get_account_service() -> AccountService:
    def uow_factory() -> AccountUnitOfWork:
        return SQLAlchemyAccountUnitOfWork(get_session_factory())

    return AccountService(policy=AccountPolicy(), uow_factory=uow_factory)


def get_authenticated_principal(
    access_credential: Annotated[str, Depends(require_bearer_access)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> PublicAuthenticatedUser:
    return session_service.current_user(access_credential)

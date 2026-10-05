from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from finance_ai.auth.dependencies import get_session_service, require_bearer_access
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.auth.session_service import SessionService
from finance_ai.dashboard.repository import DashboardRepository
from finance_ai.dashboard.service import DashboardService
from finance_ai.db.session import get_db_session


def get_dashboard_repository(
    session: Annotated[Session, Depends(get_db_session)],
) -> DashboardRepository:
    return DashboardRepository(session)


def get_dashboard_service(
    repository: Annotated[DashboardRepository, Depends(get_dashboard_repository)],
) -> DashboardService:
    return DashboardService(repository)


def get_authenticated_principal(
    access_credential: Annotated[str, Depends(require_bearer_access)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> PublicAuthenticatedUser:
    return session_service.current_user(access_credential)

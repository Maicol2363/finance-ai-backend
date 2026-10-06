from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.errors import ProblemResponse
from finance_ai.dashboard.dependencies import (
    get_authenticated_principal,
    get_dashboard_service,
)
from finance_ai.dashboard.schemas import DashboardSummaryResponse
from finance_ai.dashboard.service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

DASHBOARD_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemResponse},
    422: {"model": ProblemResponse},
    500: {"model": ProblemResponse},
}


@router.get(
    "/summary",
    response_model=DashboardSummaryResponse,
    responses=DASHBOARD_ERROR_RESPONSES,
)
def get_dashboard_summary(
    start_date: Annotated[date, Query(description="Start date (YYYY-MM-DD)")],
    end_date: Annotated[date, Query(description="End date (YYYY-MM-DD)")],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[DashboardService, Depends(get_dashboard_service)],
    currency: Annotated[str, Query(min_length=3, max_length=10)] = "COP",
    tz: Annotated[str, Query()] = "America/Bogota",
) -> DashboardSummaryResponse:
    return service.get_summary(
        user_id=principal.user_id,
        start_date=start_date,
        end_date=end_date,
        currency=currency.upper(),
        tz=tz,
    )

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response, status

from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.budgets.dependencies import get_authenticated_principal, get_budget_service
from finance_ai.budgets.schemas import (
    BudgetCreateRequest,
    BudgetListResponse,
    BudgetResponse,
    BudgetUpdateRequest,
)
from finance_ai.budgets.service import BudgetService
from finance_ai.core.errors import ProblemResponse

router = APIRouter(prefix="/budgets", tags=["Budgets"])

BUDGET_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemResponse},
    404: {"model": ProblemResponse},
    413: {"model": ProblemResponse},
    415: {"model": ProblemResponse},
    422: {"model": ProblemResponse},
    500: {"model": ProblemResponse},
    503: {"model": ProblemResponse},
}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=BudgetResponse,
    responses=BUDGET_ERROR_RESPONSES,
)
def create_budget(
    payload: BudgetCreateRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[BudgetService, Depends(get_budget_service)],
) -> BudgetResponse:
    return BudgetResponse.from_result(
        service.create(owner_id=principal.user_id, command=payload.to_command())
    )


@router.get("", response_model=BudgetListResponse, responses=BUDGET_ERROR_RESPONSES)
def list_budgets(
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[BudgetService, Depends(get_budget_service)],
) -> BudgetListResponse:
    return BudgetListResponse.from_result(
        service.list(
            owner_id=principal.user_id,
            preferred_currency_code=principal.preferred_currency,
        )
    )


@router.get(
    "/{budget_id}",
    response_model=BudgetResponse,
    responses=BUDGET_ERROR_RESPONSES,
)
def get_budget(
    budget_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[BudgetService, Depends(get_budget_service)],
) -> BudgetResponse:
    return BudgetResponse.from_result(service.get(owner_id=principal.user_id, budget_id=budget_id))


@router.patch(
    "/{budget_id}",
    response_model=BudgetResponse,
    responses=BUDGET_ERROR_RESPONSES,
)
def update_budget(
    budget_id: Annotated[int, Path(gt=0)],
    payload: BudgetUpdateRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[BudgetService, Depends(get_budget_service)],
) -> BudgetResponse:
    return BudgetResponse.from_result(
        service.update(
            owner_id=principal.user_id,
            budget_id=budget_id,
            command=payload.to_command(),
        )
    )


@router.delete(
    "/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=BUDGET_ERROR_RESPONSES,
)
def delete_budget(
    budget_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[BudgetService, Depends(get_budget_service)],
) -> Response:
    service.delete(owner_id=principal.user_id, budget_id=budget_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

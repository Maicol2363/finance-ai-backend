from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response, status

from finance_ai.accounts.dependencies import get_authenticated_principal
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.errors import ProblemResponse
from finance_ai.incomes.dependencies import get_income_service
from finance_ai.incomes.schemas import IncomeListResponse, IncomeResponse, IncomeWriteRequest
from finance_ai.incomes.service import IncomeService

router = APIRouter(prefix="/incomes", tags=["Incomes"])

INCOME_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ProblemResponse} for code in (401, 404, 413, 415, 422, 500, 503)
}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=IncomeResponse,
    responses=INCOME_ERROR_RESPONSES,
)
def create_income(
    payload: IncomeWriteRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[IncomeService, Depends(get_income_service)],
) -> IncomeResponse:
    return IncomeResponse.from_record(service.create(owner_id=principal.user_id, request=payload))


@router.get("", response_model=IncomeListResponse, responses=INCOME_ERROR_RESPONSES)
def list_incomes(
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[IncomeService, Depends(get_income_service)],
    account_id: Annotated[int | None, Query(gt=0)] = None,
) -> IncomeListResponse:
    return IncomeListResponse(
        items=[
            IncomeResponse.from_record(record)
            for record in service.list(owner_id=principal.user_id, account_id=account_id)
        ]
    )


@router.get("/{income_id}", response_model=IncomeResponse, responses=INCOME_ERROR_RESPONSES)
def get_income(
    income_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[IncomeService, Depends(get_income_service)],
) -> IncomeResponse:
    return IncomeResponse.from_record(service.get(owner_id=principal.user_id, income_id=income_id))


@router.put("/{income_id}", response_model=IncomeResponse, responses=INCOME_ERROR_RESPONSES)
def update_income(
    income_id: Annotated[int, Path(gt=0)],
    payload: IncomeWriteRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[IncomeService, Depends(get_income_service)],
) -> IncomeResponse:
    return IncomeResponse.from_record(
        service.update(owner_id=principal.user_id, income_id=income_id, request=payload)
    )


@router.delete(
    "/{income_id}", status_code=status.HTTP_204_NO_CONTENT, responses=INCOME_ERROR_RESPONSES
)
def delete_income(
    income_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[IncomeService, Depends(get_income_service)],
) -> Response:
    service.delete(owner_id=principal.user_id, income_id=income_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

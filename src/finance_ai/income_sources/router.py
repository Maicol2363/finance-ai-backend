from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response, status

from finance_ai.core.errors import ProblemResponse
from finance_ai.income_sources.dependencies import (
    get_current_user_id,
    get_income_source_service,
)
from finance_ai.income_sources.schemas import (
    CreateIncomeSourceRequest,
    IncomeSourceListResponse,
    IncomeSourceResponse,
    RenameIncomeSourceRequest,
)
from finance_ai.income_sources.service import IncomeSourceService

router = APIRouter(prefix="/income-sources", tags=["Income Sources"])

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemResponse},
    404: {"model": ProblemResponse},
    409: {"model": ProblemResponse},
    422: {"model": ProblemResponse},
    500: {"model": ProblemResponse},
    503: {"model": ProblemResponse},
}


@router.get("", response_model=IncomeSourceListResponse, responses=ERROR_RESPONSES)
def list_income_sources(
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[IncomeSourceService, Depends(get_income_source_service)],
) -> IncomeSourceListResponse:
    return IncomeSourceListResponse(
        items=[IncomeSourceResponse.from_record(record) for record in service.list(user_id=user_id)]
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=IncomeSourceResponse,
    responses=ERROR_RESPONSES,
)
def create_income_source(
    payload: CreateIncomeSourceRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[IncomeSourceService, Depends(get_income_source_service)],
) -> IncomeSourceResponse:
    return IncomeSourceResponse.from_record(service.create(user_id=user_id, name=payload.name))


@router.patch(
    "/{income_source_id}",
    response_model=IncomeSourceResponse,
    responses=ERROR_RESPONSES,
)
def rename_income_source(
    income_source_id: Annotated[int, Path(ge=1)],
    payload: RenameIncomeSourceRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[IncomeSourceService, Depends(get_income_source_service)],
) -> IncomeSourceResponse:
    return IncomeSourceResponse.from_record(
        service.rename(user_id=user_id, income_source_id=income_source_id, name=payload.name)
    )


@router.delete(
    "/{income_source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=ERROR_RESPONSES,
)
def delete_income_source(
    income_source_id: Annotated[int, Path(ge=1)],
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[IncomeSourceService, Depends(get_income_source_service)],
) -> Response:
    service.delete(user_id=user_id, income_source_id=income_source_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

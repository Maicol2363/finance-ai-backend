from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response, status

from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.categories.dependencies import (
    get_authenticated_principal,
    get_category_service,
)
from finance_ai.categories.schemas import (
    CategoryCreateRequest,
    CategoryListResponse,
    CategoryOrderRequest,
    CategoryResponse,
    CategoryUpdateRequest,
)
from finance_ai.categories.service import CategoryService
from finance_ai.core.errors import ProblemResponse

router = APIRouter(prefix="/categories", tags=["Categories"])

CATEGORY_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemResponse},
    404: {"model": ProblemResponse},
    409: {"model": ProblemResponse},
    413: {"model": ProblemResponse},
    415: {"model": ProblemResponse},
    422: {"model": ProblemResponse},
    500: {"model": ProblemResponse},
    503: {"model": ProblemResponse},
}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=CategoryResponse,
    responses=CATEGORY_ERROR_RESPONSES,
)
def create_category(
    payload: CategoryCreateRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[CategoryService, Depends(get_category_service)],
) -> CategoryResponse:
    return CategoryResponse.from_result(
        service.create(owner_id=principal.user_id, command=payload.to_command())
    )


@router.get("", response_model=CategoryListResponse, responses=CATEGORY_ERROR_RESPONSES)
def list_categories(
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[CategoryService, Depends(get_category_service)],
) -> CategoryListResponse:
    return CategoryListResponse.from_result(service.list(owner_id=principal.user_id))


@router.put("/order", response_model=CategoryListResponse, responses=CATEGORY_ERROR_RESPONSES)
def reorder_categories(
    payload: CategoryOrderRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[CategoryService, Depends(get_category_service)],
) -> CategoryListResponse:
    return CategoryListResponse.from_result(
        service.reorder(owner_id=principal.user_id, category_ids=payload.category_ids)
    )


@router.get("/{category_id}", response_model=CategoryResponse, responses=CATEGORY_ERROR_RESPONSES)
def get_category(
    category_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[CategoryService, Depends(get_category_service)],
) -> CategoryResponse:
    return CategoryResponse.from_result(
        service.get(owner_id=principal.user_id, category_id=category_id)
    )


@router.patch("/{category_id}", response_model=CategoryResponse, responses=CATEGORY_ERROR_RESPONSES)
def update_category(
    category_id: Annotated[int, Path(gt=0)],
    payload: CategoryUpdateRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[CategoryService, Depends(get_category_service)],
) -> CategoryResponse:
    return CategoryResponse.from_result(
        service.update(
            owner_id=principal.user_id,
            category_id=category_id,
            command=payload.to_command(),
        )
    )


@router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=CATEGORY_ERROR_RESPONSES,
)
def delete_category(
    category_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[CategoryService, Depends(get_category_service)],
) -> Response:
    service.delete(owner_id=principal.user_id, category_id=category_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

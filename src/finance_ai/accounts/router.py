from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response, status

from finance_ai.accounts.dependencies import (
    get_account_service,
    get_authenticated_principal,
)
from finance_ai.accounts.schemas import (
    AccountCreateRequest,
    AccountListResponse,
    AccountResponse,
    AccountStatusRequest,
    AccountUpdateRequest,
)
from finance_ai.accounts.service import AccountService
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.errors import ProblemResponse

router = APIRouter(prefix="/accounts", tags=["Accounts"])

ACCOUNT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
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
    response_model=AccountResponse,
    responses=ACCOUNT_ERROR_RESPONSES,
)
def create_account(
    payload: AccountCreateRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[AccountService, Depends(get_account_service)],
) -> AccountResponse:
    return AccountResponse.from_result(
        service.create(owner_id=principal.user_id, command=payload.to_command())
    )


@router.get(
    "",
    response_model=AccountListResponse,
    responses=ACCOUNT_ERROR_RESPONSES,
)
def list_accounts(
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[AccountService, Depends(get_account_service)],
    include_inactive: Annotated[bool, Query()] = False,
) -> AccountListResponse:
    return AccountListResponse.from_result(
        service.list(
            owner_id=principal.user_id,
            preferred_currency_code=principal.preferred_currency,
            include_inactive=include_inactive,
        )
    )


@router.get(
    "/{account_id}",
    response_model=AccountResponse,
    responses=ACCOUNT_ERROR_RESPONSES,
)
def get_account(
    account_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[AccountService, Depends(get_account_service)],
) -> AccountResponse:
    return AccountResponse.from_result(
        service.get(owner_id=principal.user_id, account_id=account_id)
    )


@router.patch(
    "/{account_id}",
    response_model=AccountResponse,
    responses=ACCOUNT_ERROR_RESPONSES,
)
def update_account(
    account_id: Annotated[int, Path(gt=0)],
    payload: AccountUpdateRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[AccountService, Depends(get_account_service)],
) -> AccountResponse:
    return AccountResponse.from_result(
        service.update(
            owner_id=principal.user_id,
            account_id=account_id,
            command=payload.to_command(),
        )
    )


@router.patch(
    "/{account_id}/status",
    response_model=AccountResponse,
    responses=ACCOUNT_ERROR_RESPONSES,
)
def set_account_status(
    account_id: Annotated[int, Path(gt=0)],
    payload: AccountStatusRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[AccountService, Depends(get_account_service)],
) -> AccountResponse:
    return AccountResponse.from_result(
        service.set_active(
            owner_id=principal.user_id,
            account_id=account_id,
            is_active=payload.is_active,
        )
    )


@router.delete(
    "/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ACCOUNT_ERROR_RESPONSES,
)
def delete_account(
    account_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[AccountService, Depends(get_account_service)],
) -> Response:
    service.delete(owner_id=principal.user_id, account_id=account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

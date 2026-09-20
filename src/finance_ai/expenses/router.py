"""Authenticated HTTP routes for expense capture, correction, and financial history."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response, status

from finance_ai.auth.dependencies import get_session_service, require_bearer_access
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.auth.session_service import SessionService
from finance_ai.core.errors import ProblemResponse
from finance_ai.expenses.dependencies import get_expense_service
from finance_ai.expenses.schemas import (
    ExpenseCreateRequest,
    ExpenseListResponse,
    ExpenseResponse,
    ExpenseUpdateRequest,
    HistoryItemResponse,
    HistoryPeriod,
    HistoryResponse,
    TransactionType,
)
from finance_ai.expenses.service import ExpenseService

router = APIRouter(tags=["Expenses", "History"])

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemResponse},
    404: {"model": ProblemResponse},
    409: {"model": ProblemResponse},
    413: {"model": ProblemResponse},
    415: {"model": ProblemResponse},
    422: {"model": ProblemResponse},
    500: {"model": ProblemResponse},
    503: {"model": ProblemResponse},
}


def get_authenticated_principal(
    access_credential: Annotated[str, Depends(require_bearer_access)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> PublicAuthenticatedUser:
    return session_service.current_user(access_credential)


@router.post(
    "/expenses",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
def create_expense(
    payload: ExpenseCreateRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[ExpenseService, Depends(get_expense_service)],
) -> ExpenseResponse:
    return ExpenseResponse.from_record(
        service.create(
            owner_id=principal.user_id,
            command=payload.to_command(),
        )
    )


@router.get(
    "/expenses",
    response_model=ExpenseListResponse,
    responses=ERROR_RESPONSES,
)
def list_expenses(
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[ExpenseService, Depends(get_expense_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ExpenseListResponse:
    result = service.list(owner_id=principal.user_id, page=page, page_size=page_size)
    return ExpenseListResponse(
        items=[ExpenseResponse.from_record(item) for item in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        has_next=result.has_next,
    )


@router.get(
    "/expenses/{expense_id}",
    response_model=ExpenseResponse,
    responses=ERROR_RESPONSES,
)
def get_expense(
    expense_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[ExpenseService, Depends(get_expense_service)],
) -> ExpenseResponse:
    return ExpenseResponse.from_record(
        service.get(owner_id=principal.user_id, expense_id=expense_id)
    )


@router.patch(
    "/expenses/{expense_id}",
    response_model=ExpenseResponse,
    responses=ERROR_RESPONSES,
)
def update_expense(
    expense_id: Annotated[int, Path(gt=0)],
    payload: ExpenseUpdateRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[ExpenseService, Depends(get_expense_service)],
) -> ExpenseResponse:
    return ExpenseResponse.from_record(
        service.update(
            owner_id=principal.user_id,
            expense_id=expense_id,
            command=payload.to_command(),
        )
    )


@router.delete(
    "/expenses/{expense_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ERROR_RESPONSES,
)
def delete_expense(
    expense_id: Annotated[int, Path(gt=0)],
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[ExpenseService, Depends(get_expense_service)],
) -> Response:
    service.delete(owner_id=principal.user_id, expense_id=expense_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/history",
    response_model=HistoryResponse,
    responses=ERROR_RESPONSES,
)
def get_history(
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[ExpenseService, Depends(get_expense_service)],
    search: Annotated[str | None, Query(max_length=200)] = None,
    transaction_type: Annotated[TransactionType | None, Query()] = None,
    category_id: Annotated[int | None, Query(gt=0)] = None,
    account_id: Annotated[int | None, Query(gt=0)] = None,
    period: Annotated[HistoryPeriod | None, Query()] = None,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> HistoryResponse:
    result = service.history(
        owner_id=principal.user_id,
        search=search,
        transaction_type=None if transaction_type is None else transaction_type.value,
        category_id=category_id,
        account_id=account_id,
        period=period,
        start_date=start_date,
        end_date=end_date,
        page=page,
        page_size=page_size,
    )
    return HistoryResponse(
        items=[HistoryItemResponse.from_record(item) for item in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        has_next=result.has_next,
    )

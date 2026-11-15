"""Administrator API. Every route needs an active admin with an MFA-verified session.

The dependency ``get_admin_user`` re-checks the current role, status, authenticator and
session assurance on each request; the frontend route guard is only presentation.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from finance_ai.admin.dependencies import (
    AccountIdPath,
    AccountService,
    ReadingAdmin,
    WritingAdmin,
    correlation_id,
)
from finance_ai.admin.schemas import (
    MAX_BIGINT,
    AdminAccount,
    ReactivateAccountRequest,
    SuspendAccountRequest,
)
from finance_ai.core.errors import ProblemResponse

router = APIRouter(prefix="/staff/admin", tags=["Administrator"])

_COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemResponse},
    403: {"model": ProblemResponse},
    429: {"model": ProblemResponse},
    503: {"model": ProblemResponse},
}


@router.get(
    "/accounts/lookup",
    response_model=AdminAccount,
    summary="Look Up a Login Account (ADM-01)",
    description=(
        "Exact email or account ID lookup of one customer or staff login account. Returns "
        "allowlisted metadata only: never balances, transactions, receipts, credentials or "
        "session values. The record is a login account, not a financial account."
    ),
    responses={**_COMMON_ERRORS, 404: {"model": ProblemResponse}, 422: {"model": ProblemResponse}},
)
def lookup_account(
    admin: ReadingAdmin,
    service: AccountService,
    email: Annotated[str | None, Query(max_length=320)] = None,
    account_id: Annotated[int | None, Query(alias="id", ge=1, le=MAX_BIGINT)] = None,
) -> AdminAccount:
    del admin
    return service.lookup(email=email, account_id=account_id)


@router.get(
    "/accounts/{account_id}",
    response_model=AdminAccount,
    summary="View a Login Account (ADM-01)",
    responses={**_COMMON_ERRORS, 404: {"model": ProblemResponse}},
)
def get_account(
    account_id: AccountIdPath, admin: ReadingAdmin, service: AccountService
) -> AdminAccount:
    del admin
    return service.detail(account_id)


@router.post(
    "/accounts/{account_id}/suspend",
    response_model=AdminAccount,
    summary="Suspend a Login Account (ADM-02)",
    description=(
        "Requires explicit confirmation and a non-blank reason. Revokes the account's live "
        "sessions immediately. Never the acting administrator and never the last active "
        "administrator. Financial and support data are preserved."
    ),
    responses={
        **_COMMON_ERRORS,
        404: {"model": ProblemResponse},
        409: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
    },
)
def suspend_account(
    account_id: AccountIdPath,
    payload: SuspendAccountRequest,
    request: Request,
    admin: WritingAdmin,
    service: AccountService,
) -> AdminAccount:
    return service.suspend(
        actor_id=admin.id,
        target_id=account_id,
        reason=payload.reason,
        correlation_id=correlation_id(request),
    )


@router.post(
    "/accounts/{account_id}/reactivate",
    response_model=AdminAccount,
    summary="Reactivate a Suspended Login Account (ADM-02)",
    description=(
        "Restores access eligibility only. Sessions issued before the suspension are never "
        "restored; the person must sign in again (and complete MFA where required)."
    ),
    responses={
        **_COMMON_ERRORS,
        404: {"model": ProblemResponse},
        409: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
    },
)
def reactivate_account(
    account_id: AccountIdPath,
    payload: ReactivateAccountRequest,
    request: Request,
    admin: WritingAdmin,
    service: AccountService,
) -> AdminAccount:
    del payload
    return service.reactivate(
        actor_id=admin.id, target_id=account_id, correlation_id=correlation_id(request)
    )

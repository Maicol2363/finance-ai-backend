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
    StaffService,
    WritingAdmin,
    correlation_id,
)
from finance_ai.admin.schemas import (
    MAX_BIGINT,
    AdminAccount,
    ChangeStaffRoleRequest,
    CreateStaffInvitationRequest,
    ReactivateAccountRequest,
    StaffAccountView,
    StaffInvitationCreated,
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


@router.post(
    "/staff/invitations",
    status_code=201,
    response_model=StaffInvitationCreated,
    summary="Invite a Staff Member (ADM-03)",
    description=(
        "Creates an expiring, single-use invitation for a support agent or security auditor. "
        "The one-time activation token is returned once in this response and never stored in "
        "clear text; the invitee chooses their own password at activation. The admin role "
        "cannot be requested (an unknown or forged role is rejected)."
    ),
    responses={
        **_COMMON_ERRORS,
        409: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
    },
)
def create_staff_invitation(
    payload: CreateStaffInvitationRequest,
    request: Request,
    admin: WritingAdmin,
    service: StaffService,
) -> StaffInvitationCreated:
    return service.create_invitation(
        actor_id=admin.id,
        email=payload.email,
        role=payload.role,
        correlation_id=correlation_id(request),
    )


@router.get(
    "/staff/{staff_id}",
    response_model=StaffAccountView,
    summary="Inspect a Staff Account Role (ADM-04)",
    responses={**_COMMON_ERRORS, 404: {"model": ProblemResponse}},
)
def get_staff_account(
    staff_id: AccountIdPath, admin: ReadingAdmin, service: StaffService
) -> StaffAccountView:
    del admin
    return service.view(staff_id)


@router.post(
    "/staff/{staff_id}/role",
    response_model=StaffAccountView,
    summary="Change a Staff Role (ADM-04)",
    description=(
        "Switches a staff account between support_agent and security_auditor after explicit "
        "confirmation. Own role, administrators and customers cannot be changed here. The "
        "live sessions of that person are revoked and the change is audited with the previous "
        "and new role."
    ),
    responses={
        **_COMMON_ERRORS,
        404: {"model": ProblemResponse},
        409: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
    },
)
def change_staff_role(
    staff_id: AccountIdPath,
    payload: ChangeStaffRoleRequest,
    request: Request,
    admin: WritingAdmin,
    service: StaffService,
) -> StaffAccountView:
    return service.change_role(
        actor_id=admin.id,
        staff_id=staff_id,
        new_role=payload.role,
        correlation_id=correlation_id(request),
    )

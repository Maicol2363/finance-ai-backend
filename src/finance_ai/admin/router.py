"""Administrator API. Every route needs an active admin with an MFA-verified session.

The dependency ``get_admin_user`` re-checks the current role, status, authenticator and
session assurance on each request; the frontend route guard is only presentation.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response

from finance_ai.admin.dependencies import (
    AccountIdPath,
    AccountService,
    ReadingAdmin,
    StaffService,
    TicketService,
    WritingAdmin,
    correlation_id,
)
from finance_ai.admin.schemas import (
    AccountLookupRequest,
    AdminAccount,
    AdminCase,
    ChangeStaffRoleRequest,
    ClaimTicketRequest,
    CreateStaffInvitationRequest,
    EscalatedTicketItem,
    EscalationScope,
    ReactivateAccountRequest,
    ReturnTicketRequest,
    ReturnTicketResult,
    StaffAccountView,
    StaffInvitationCreated,
    SuspendAccountRequest,
)
from finance_ai.core.errors import ProblemResponse
from finance_ai.support.schemas import MessageCreate, TicketId, TransitionRequest

router = APIRouter(prefix="/staff/admin", tags=["Administrator"])

_COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemResponse},
    403: {"model": ProblemResponse},
    429: {"model": ProblemResponse},
    503: {"model": ProblemResponse},
}


@router.post(
    "/accounts/lookup",
    response_model=AdminAccount,
    summary="Look Up a Login Account (ADM-01)",
    description=(
        "Exact email or account ID lookup of one customer or staff login account, sent in the "
        "request body so the address never appears in a URL or proxy access log. Returns "
        "allowlisted metadata only: never balances, transactions, receipts, credentials or "
        "session values. The record is a login account, not a financial account."
    ),
    responses={**_COMMON_ERRORS, 404: {"model": ProblemResponse}, 422: {"model": ProblemResponse}},
)
def lookup_account(
    payload: AccountLookupRequest,
    admin: ReadingAdmin,
    service: AccountService,
) -> AdminAccount:
    del admin
    return service.lookup(email=payload.email, account_id=payload.id)


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


@router.get(
    "/tickets/escalated",
    response_model=list[EscalatedTicketItem],
    summary="List Escalated Tickets (ADM-05)",
    description="Escalated cases only, oldest first, bounded to 100 rows.",
    responses=_COMMON_ERRORS,
)
def list_escalated_tickets(
    admin: ReadingAdmin,
    service: TicketService,
    scope: Annotated[EscalationScope, Query()] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[EscalatedTicketItem]:
    return service.list_escalated(admin_id=admin.id, scope=scope, limit=limit)


@router.get(
    "/tickets/{ticket_id}",
    response_model=AdminCase,
    summary="View an Escalated Case (ADM-05)",
    description=(
        "Escalation reason, allowed customer metadata, conversation and activity history of "
        "one escalated ticket. No transactions, balances, receipts or credentials are exposed."
    ),
    responses={**_COMMON_ERRORS, 404: {"model": ProblemResponse}},
)
def get_escalated_case(
    ticket_id: TicketId, admin: ReadingAdmin, service: TicketService
) -> AdminCase:
    return service.get_case(admin_id=admin.id, ticket_id=ticket_id)


@router.post(
    "/tickets/{ticket_id}/claim",
    response_model=AdminCase,
    summary="Claim an Escalated Case (ADM-05)",
    description="Exactly one administrator can own a case; a concurrent claim gets a 409.",
    responses={**_COMMON_ERRORS, 404: {"model": ProblemResponse}, 409: {"model": ProblemResponse}},
)
def claim_escalated_case(
    ticket_id: TicketId,
    payload: ClaimTicketRequest,
    request: Request,
    admin: WritingAdmin,
    service: TicketService,
) -> AdminCase:
    del payload
    return service.claim(
        admin_id=admin.id, ticket_id=ticket_id, correlation_id=correlation_id(request)
    )


@router.post(
    "/tickets/{ticket_id}/messages",
    status_code=204,
    response_class=Response,
    summary="Reply to the Customer (ADM-05)",
    description=(
        "Customer-visible message in the existing ticket conversation, only by the owning "
        "administrator. There are no private notes or internal messaging."
    ),
    responses={
        **_COMMON_ERRORS,
        404: {"model": ProblemResponse},
        409: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
    },
)
def reply_to_customer(
    ticket_id: TicketId,
    payload: MessageCreate,
    request: Request,
    admin: WritingAdmin,
    service: TicketService,
) -> Response:
    service.reply(
        admin_id=admin.id,
        ticket_id=ticket_id,
        body=payload.body,
        correlation_id=correlation_id(request),
    )
    return Response(status_code=204)


@router.post(
    "/tickets/{ticket_id}/resolve",
    status_code=204,
    response_class=Response,
    summary="Resolve an Escalated Case (ADM-05)",
    description="Requires a resolution summary, which the owning active customer can read.",
    responses={
        **_COMMON_ERRORS,
        404: {"model": ProblemResponse},
        409: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
    },
)
def resolve_escalated_case(
    ticket_id: TicketId,
    payload: TransitionRequest,
    request: Request,
    admin: WritingAdmin,
    service: TicketService,
) -> Response:
    service.resolve(
        admin_id=admin.id,
        ticket_id=ticket_id,
        summary=payload.summary,
        correlation_id=correlation_id(request),
    )
    return Response(status_code=204)


@router.post(
    "/tickets/{ticket_id}/return",
    response_model=ReturnTicketResult,
    summary="Return a Case to Support (ADM-05)",
    description=(
        "Requires a staff-only return reason. If the originating agent is still an active "
        "support agent the case returns to that agent as in_progress; otherwise it returns "
        "unassigned to the open queue. Status and assignment change atomically."
    ),
    responses={
        **_COMMON_ERRORS,
        404: {"model": ProblemResponse},
        409: {"model": ProblemResponse},
        422: {"model": ProblemResponse},
    },
)
def return_case_to_support(
    ticket_id: TicketId,
    payload: ReturnTicketRequest,
    request: Request,
    admin: WritingAdmin,
    service: TicketService,
) -> ReturnTicketResult:
    return service.return_to_support(
        admin_id=admin.id,
        ticket_id=ticket_id,
        reason=payload.reason,
        correlation_id=correlation_id(request),
    )

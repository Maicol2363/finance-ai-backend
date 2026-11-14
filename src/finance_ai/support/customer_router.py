from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from finance_ai.accounts.dependencies import get_authenticated_principal
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.support.dependencies import Workflow
from finance_ai.support.schemas import (
    CustomerConversation,
    CustomerTicket,
    CustomerTicketList,
    MessageCreate,
    TicketCreate,
    TicketId,
)

router = APIRouter(prefix="/support/tickets", tags=["Customer support tickets"])
Customer = Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)]


def correlation(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "unavailable"))


@router.get("", response_model=CustomerTicketList)
def list_tickets(customer: Customer, workflow: Workflow) -> CustomerTicketList:
    return CustomerTicketList(tickets=workflow.list_own(customer.user_id))


@router.post("", response_model=CustomerTicket, status_code=201)
def create_ticket(
    data: TicketCreate, request: Request, customer: Customer, workflow: Workflow
) -> CustomerTicket:
    return workflow.create(customer.user_id, data, correlation(request), staff=False)


@router.get("/{ticket_id}", response_model=CustomerConversation)
def read_ticket(
    ticket_id: TicketId, customer: Customer, workflow: Workflow
) -> CustomerConversation:
    return workflow.read(customer.user_id, ticket_id, staff=False)


@router.post("/{ticket_id}/messages", status_code=204)
def send_message(
    ticket_id: TicketId,
    data: MessageCreate,
    request: Request,
    customer: Customer,
    workflow: Workflow,
) -> Response:
    workflow.message(customer.user_id, ticket_id, data.body, correlation(request), staff=False)
    return Response(status_code=204)

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from finance_ai.support.customer_router import correlation
from finance_ai.support.dependencies import SupportAgent, SupportService, Workflow
from finance_ai.support.schemas import (
    AgentTicketCreate,
    CustomerLookup,
    CustomerMetadata,
    CustomerTicket,
    MessageCreate,
    StaffConversation,
    TicketClaimResult,
    TicketDetail,
    TicketId,
    TicketQueueItem,
    TicketStatus,
    TransitionRequest,
)

router = APIRouter(prefix="/staff/support/tickets", tags=["Staff support tickets"])


@router.get("", response_model=list[TicketQueueItem])
def list_tickets(
    service: SupportService,
    agent: SupportAgent,
    status: Annotated[TicketStatus | None, Query()] = None,
) -> list[TicketQueueItem]:
    return service.list_queue(agent, status)


@router.get("/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket_id: TicketId, service: SupportService, agent: SupportAgent) -> TicketDetail:
    return service.get_assigned(agent, ticket_id)


@router.post("/{ticket_id}/claim", response_model=TicketClaimResult)
def claim_ticket(
    ticket_id: TicketId,
    request: Request,
    service: SupportService,
    agent: SupportAgent,
) -> TicketClaimResult:
    return service.claim(
        agent,
        ticket_id,
        str(getattr(request.state, "correlation_id", "unavailable")),
    )


@router.post("/customer-lookup", response_model=CustomerMetadata)
def lookup_customer(
    data: CustomerLookup, request: Request, agent: SupportAgent, workflow: Workflow
) -> CustomerMetadata:
    return workflow.lookup(agent.id, data.identifier, correlation(request))


@router.post("", response_model=CustomerTicket, status_code=201)
def create_ticket(
    data: AgentTicketCreate, request: Request, agent: SupportAgent, workflow: Workflow
) -> CustomerTicket:
    return workflow.create(agent.id, data, correlation(request), staff=True)


@router.get("/{ticket_id}/conversation", response_model=StaffConversation)
def read_conversation(
    ticket_id: TicketId, agent: SupportAgent, workflow: Workflow
) -> StaffConversation:
    result = workflow.read(agent.id, ticket_id, staff=True)
    assert isinstance(result, StaffConversation)
    return result


@router.post("/{ticket_id}/messages", status_code=204)
def send_message(
    ticket_id: TicketId,
    data: MessageCreate,
    request: Request,
    agent: SupportAgent,
    workflow: Workflow,
) -> Response:
    workflow.message(agent.id, ticket_id, data.body, correlation(request), staff=True)
    return Response(status_code=204)


@router.post("/{ticket_id}/escalate", status_code=204)
def escalate_ticket(
    ticket_id: TicketId,
    data: TransitionRequest,
    request: Request,
    agent: SupportAgent,
    workflow: Workflow,
) -> Response:
    workflow.transition(agent.id, ticket_id, data.summary, correlation(request), escalate=True)
    return Response(status_code=204)


@router.post("/{ticket_id}/resolve", status_code=204)
def resolve_ticket(
    ticket_id: TicketId,
    data: TransitionRequest,
    request: Request,
    agent: SupportAgent,
    workflow: Workflow,
) -> Response:
    workflow.transition(agent.id, ticket_id, data.summary, correlation(request), escalate=False)
    return Response(status_code=204)

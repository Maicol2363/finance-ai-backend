from typing import Annotated

from fastapi import APIRouter, Query, Request

from finance_ai.support.dependencies import SupportAgent, SupportService
from finance_ai.support.schemas import (
    TicketClaimResult,
    TicketDetail,
    TicketQueueItem,
    TicketStatus,
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
def get_ticket(ticket_id: int, service: SupportService, agent: SupportAgent) -> TicketDetail:
    return service.get_assigned(agent, ticket_id)


@router.post("/{ticket_id}/claim", response_model=TicketClaimResult)
def claim_ticket(
    ticket_id: int,
    request: Request,
    service: SupportService,
    agent: SupportAgent,
) -> TicketClaimResult:
    return service.claim(
        agent,
        ticket_id,
        str(getattr(request.state, "correlation_id", "unavailable")),
    )

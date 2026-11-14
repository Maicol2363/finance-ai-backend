from typing import Annotated

from fastapi import Depends, Request

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.db.session import get_session_factory
from finance_ai.staff_auth.dependencies import get_staff_access_user
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.rate_limit import enforce_support_limit
from finance_ai.support.service import SupportTicketService
from finance_ai.support.workflow import TicketWorkflow


def get_support_ticket_service() -> SupportTicketService:
    return SupportTicketService(get_session_factory(), SecurityAuditWriter())


SupportAgent = Annotated[StaffSessionUser, Depends(get_staff_access_user)]
SupportService = Annotated[SupportTicketService, Depends(get_support_ticket_service)]


def get_ticket_workflow(request: Request) -> TicketWorkflow:
    enforce_support_limit(request)
    return TicketWorkflow(get_session_factory())


Workflow = Annotated[TicketWorkflow, Depends(get_ticket_workflow)]

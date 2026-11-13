from typing import Annotated

from fastapi import Depends

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.db.session import get_session_factory
from finance_ai.staff_auth.dependencies import get_staff_access_user
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.service import SupportTicketService


def get_support_ticket_service() -> SupportTicketService:
    return SupportTicketService(get_session_factory(), SecurityAuditWriter())


SupportAgent = Annotated[StaffSessionUser, Depends(get_staff_access_user)]
SupportService = Annotated[SupportTicketService, Depends(get_support_ticket_service)]

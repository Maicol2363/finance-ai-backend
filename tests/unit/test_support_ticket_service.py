import pytest

from finance_ai.core.errors import StaffAccessDeniedError
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.schemas import TicketStatus
from finance_ai.support.service import SupportTicketService


def test_queue_requires_support_agent() -> None:
    service = SupportTicketService(lambda: None, audit_writer=None)  # type: ignore[arg-type]
    user = StaffSessionUser(id=1, email="user@example.test", role="user")
    with pytest.raises(StaffAccessDeniedError):
        service.list_queue(user, TicketStatus.OPEN)


def test_status_values_are_the_approved_filter_contract() -> None:
    assert {status.value for status in TicketStatus} == {
        "open",
        "in_progress",
        "escalated",
        "resolved",
    }

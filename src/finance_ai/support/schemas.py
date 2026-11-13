from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class TicketQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    subject: str
    status: TicketStatus
    created_at: datetime
    assigned_staff_user_id: int | None


class TicketDetail(TicketQueueItem):
    category: str
    description: str


class TicketClaimResult(TicketQueueItem):
    pass

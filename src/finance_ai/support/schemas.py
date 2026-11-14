from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator


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


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Category = Literal["general", "account", "technical", "other"]


class SupportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def safe_text(cls, value: object) -> object:
        if isinstance(value, str) and any(
            (ord(char) < 32 and char not in "\n\r\t") or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        ):
            raise ValueError("Unsupported text characters")
        return value


class TicketCreate(SupportInput):
    model_config = ConfigDict(extra="forbid")
    subject: ShortText
    category: Category
    description: LongText


class CustomerLookup(SupportInput):
    model_config = ConfigDict(extra="forbid")
    identifier: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)
    ]


class AgentTicketCreate(TicketCreate):
    customer_identifier: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)
    ]


class MessageCreate(SupportInput):
    model_config = ConfigDict(extra="forbid")
    body: LongText


class TransitionRequest(SupportInput):
    model_config = ConfigDict(extra="forbid")
    summary: LongText


class CustomerMetadata(BaseModel):
    id: int
    email: str
    is_active: bool


class TicketMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    author_user_id: int
    body: str
    created_at: datetime


class TicketHistory(BaseModel):
    internal_reason: str | None
    model_config = ConfigDict(from_attributes=True)
    id: int
    actor_user_id: int
    event_type: str
    previous_status: str | None
    new_status: str | None
    previous_assignee_user_id: int | None
    new_assignee_user_id: int | None
    created_at: datetime


class CustomerTicket(TicketDetail):
    resolution_summary: str | None


class CustomerConversation(BaseModel):
    ticket: CustomerTicket
    messages: list[TicketMessage]


class StaffConversation(CustomerConversation):
    customer: CustomerMetadata
    history: list[TicketHistory]


class CustomerTicketList(BaseModel):
    tickets: list[CustomerTicket]


TicketId = Annotated[int, Path(gt=0, le=9223372036854775807)]

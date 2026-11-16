"""Strict Administrator request/response models (ADM-01..ADM-05).

Requests forbid unknown fields, so a forged ``role`` (or any other hidden field) is rejected
before any service runs. Responses are explicit allowlists: no password hash, credential,
financial record or session value can be added by accident.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from finance_ai.support.schemas import (
    CustomerMetadata,
    CustomerTicket,
    LongText,
    SupportInput,
    TicketHistory,
    TicketMessage,
)

MAX_REASON_LENGTH: Final = 500
MAX_BIGINT: Final = 9_223_372_036_854_775_807
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

AccountId = Annotated[int, Field(ge=1, le=MAX_BIGINT)]
AccountStatus = Literal["active", "suspended"]


def clean_reason(value: str, *, field: str = "reason") -> str:
    """Trim a free-text reason; it must be non-blank, bounded and free of control characters."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be blank")
    if len(cleaned) > MAX_REASON_LENGTH:
        raise ValueError(f"{field} must be {MAX_REASON_LENGTH} characters or fewer")
    if _CONTROL_CHARACTERS.search(cleaned):
        raise ValueError(f"{field} contains unsupported characters")
    return cleaned


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class SuspensionRecord(BaseModel):
    """Staff-only suspension evidence for a suspended login account."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    suspended_at: datetime
    suspended_by_user_id: int


class AdminAccount(BaseModel):
    """ADM-01 allowlisted login-account metadata (never a financial account)."""

    model_config = ConfigDict(extra="forbid")

    id: int
    email: str
    role: str
    status: AccountStatus
    registered_at: datetime
    # Distinguishes this identity record from the customer's financial accounts.
    account_type: Literal["login_account"] = "login_account"
    suspension: SuspensionRecord | None = None


class AccountLookupRequest(StrictRequest):
    """Exactly one exact key. The email travels in the body, never in a URL that proxies log."""

    email: str | None = Field(default=None, max_length=320)
    id: int | None = Field(default=None, ge=1, le=MAX_BIGINT)


class SuspendAccountRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=2_000)
    confirm: Literal[True]

    @field_validator("reason")
    @classmethod
    def reason_must_be_meaningful(cls, value: str) -> str:
        return clean_reason(value)


class ReactivateAccountRequest(StrictRequest):
    confirm: Literal[True]


# The only roles the ordinary portal may assign (ADM-03/ADM-04). ``admin`` and ``user`` are not
# members of this Literal, so a forged role is a validation failure before any service runs.
PortalStaffRole = Literal["support_agent", "security_auditor"]


class CreateStaffInvitationRequest(StrictRequest):
    email: str = Field(min_length=1, max_length=320)
    role: PortalStaffRole


class StaffInvitationCreated(BaseModel):
    """Returned once to the administrator; only the token hash is stored (D1)."""

    model_config = ConfigDict(extra="forbid")

    invitation_id: int
    email: str
    role: PortalStaffRole
    expires_at: datetime
    activation_token: str


class StaffAccountView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    email: str
    role: str
    status: AccountStatus
    registered_at: datetime
    account_type: Literal["login_account"] = "login_account"
    assignable_roles: list[PortalStaffRole]


class ChangeStaffRoleRequest(StrictRequest):
    role: PortalStaffRole
    confirm: Literal[True]


# --- ADM-05 -------------------------------------------------------------------------------------

EscalationScope = Literal["all", "unclaimed", "mine"]


class EscalatedTicketItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    subject: str
    category: str
    created_at: datetime
    owner_admin_id: int | None
    originating_agent_id: int | None
    owned_by_me: bool


class EscalationOrigin(BaseModel):
    """Who escalated the case and whether Support can take it back directly."""

    model_config = ConfigDict(extra="forbid")

    user_id: int | None
    can_receive_return: bool


class AdminCase(BaseModel):
    """Administrative case context: staff-only reasons appear here and never to customers."""

    model_config = ConfigDict(extra="forbid")

    ticket: CustomerTicket
    customer: CustomerMetadata
    messages: list[TicketMessage]
    history: list[TicketHistory]
    escalation_reason: str | None
    owner_admin_id: int | None
    owned_by_me: bool
    originating_agent: EscalationOrigin


class ClaimTicketRequest(StrictRequest):
    """Claiming carries no data; the owner is always the authenticated administrator."""


class ReturnTicketRequest(SupportInput):
    reason: LongText


class ReturnTicketResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["in_progress", "open"]
    assigned_staff_user_id: int | None

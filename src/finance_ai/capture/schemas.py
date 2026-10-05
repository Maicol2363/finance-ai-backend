"""Public contract of the HU-013 text capture: request, proposal, clarification, confirmation."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_ai.incomes.schemas import IncomeAmountInput

MAX_TEXT_LENGTH = 1_000
CAPTURE_SOURCE = "text_ai"


class CaptureIntent(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"


class ProposalStatus(StrEnum):
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNRECOGNIZED = "unrecognized"
    CONFIRMED = "confirmed"
    DISCARDED = "discarded"


ClarificationField = Literal[
    "intent", "amount", "account", "destination_account", "destination_amount"
]


class TextChoice(BaseModel):
    """A tapped answer to a clarification: applied deterministically, without a new AI call."""

    model_config = ConfigDict(extra="forbid")

    field: ClarificationField
    value: Annotated[str, Field(min_length=1, max_length=40)]


class TextProposalRequest(BaseModel):
    """One chat turn: a first message, a typed correction or a tapped clarification choice."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(max_length=MAX_TEXT_LENGTH)] = ""
    client_now: dt.datetime
    proposal_id: UUID | None = None
    choice: TextChoice | None = None

    @field_validator("text")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("client_now")
    @classmethod
    def require_timezone(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("client_now must include a timezone")
        return value

    @model_validator(mode="after")
    def require_text_or_choice(self) -> TextProposalRequest:
        if self.choice is None and not self.text:
            raise ValueError("text cannot be empty")
        if self.choice is not None and self.proposal_id is None:
            raise ValueError("choice requires proposal_id")
        return self


class AccountRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    currency_code: str


class CatalogRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str


class ClarificationOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    label: str
    detail: str | None = None


class Clarification(BaseModel):
    """The single short question asked when the text is genuinely ambiguous."""

    model_config = ConfigDict(extra="forbid")

    field: ClarificationField
    question: str
    options: list[ClarificationOption] = Field(default_factory=list, max_length=8)


class TextProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    revision: int
    status: ProposalStatus
    intent: CaptureIntent | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    one_tap: bool
    original_text: str
    follow_ups: list[str] = Field(default_factory=list)
    message: str | None = None
    amount: str | None = None
    currency_code: str | None = None
    destination_amount: str | None = None
    date: dt.date | None = None
    description: str | None = None
    account: AccountRef | None = None
    destination_account: AccountRef | None = None
    category: CatalogRef | None = None
    income_source: CatalogRef | None = None
    clarification: Clarification | None = None
    notes: list[str] = Field(default_factory=list)
    created_at: dt.datetime
    expires_at: dt.datetime


class TextProposalConfirmRequest(BaseModel):
    """Final, possibly edited values. They go through the same validation as the manual forms."""

    model_config = ConfigDict(extra="forbid")

    intent: CaptureIntent
    amount: IncomeAmountInput
    date: dt.date
    description: Annotated[str, Field(max_length=500)] | None = None
    account_id: Annotated[int, Field(gt=0)]
    destination_account_id: Annotated[int, Field(gt=0)] | None = None
    destination_amount: IncomeAmountInput | None = None
    category_id: Annotated[int, Field(gt=0)] | None = None
    income_source_id: Annotated[int, Field(gt=0)] | None = None

    @field_validator("description")
    @classmethod
    def trim_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class TextCaptureConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    intent: CaptureIntent
    record_id: int
    amount: str
    currency_code: str
    date: dt.date
    description: str | None
    account_name: str
    destination_account_name: str | None = None
    replayed: bool = False

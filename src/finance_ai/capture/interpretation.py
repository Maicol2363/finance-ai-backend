"""Tolerant parser of the model's JSON answer.

The model output is untrusted input. Anything malformed degrades to "not provided" so the
deterministic grounding step asks the user instead of guessing or failing the request.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

MONEY_MAX = Decimal("9999999999.99")
# A transfer between the user's own accounts is stored in movements (18 digits), unlike
# expense and income rows (12 digits), so it accepts a wider range.
TRANSFER_MONEY_MAX = Decimal("9999999999999999.99")
_INTENTS = frozenset({"expense", "income", "transfer"})
_CENTS = Decimal("0.01")


def _clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] if cleaned else None


def _clean_amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value).strip()).quantize(_CENTS, rounding=ROUND_HALF_UP)
    except InvalidOperation, ValueError:
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return amount


class RawInterpretation(BaseModel):
    """What the model said about one message, before it is checked against the user's data."""

    model_config = ConfigDict(extra="ignore")

    intent: str = "unknown"
    confidence: float = 0.0
    amount: Decimal | None = None
    currency: str | None = None
    date: dt.date | None = None
    description: str | None = None
    account: str | None = None
    destination_account: str | None = None
    destination_amount: Decimal | None = None
    category: str | None = None
    income_source: str | None = None

    @field_validator("intent", mode="before")
    @classmethod
    def normalize_intent(cls, value: Any) -> str:
        text = value.strip().lower() if isinstance(value, str) else ""
        return text if text in _INTENTS else "unknown"

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, value: Any) -> float:
        try:
            number = float(value)
        except TypeError, ValueError:
            return 0.0
        if math.isnan(number):
            return 0.0
        return max(0.0, min(1.0, number))

    @field_validator("amount", "destination_amount", mode="before")
    @classmethod
    def normalize_amount(cls, value: Any) -> Decimal | None:
        return _clean_amount(value)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> str | None:
        text = _clean_text(value, 16)
        return text.upper() if text and len(text) == 3 and text.isalpha() else None

    @field_validator("date", mode="before")
    @classmethod
    def normalize_date(cls, value: Any) -> dt.date | None:
        if not isinstance(value, str):
            return None
        try:
            return dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: Any) -> str | None:
        return _clean_text(value, 150)

    @field_validator("account", "destination_account", "category", "income_source", mode="before")
    @classmethod
    def normalize_reference(cls, value: Any) -> str | None:
        return _clean_text(value, 100)

    @model_validator(mode="after")
    def cap_amounts_by_intent(self) -> RawInterpretation:
        """Money range depends on what is recorded: transfers live in wider movements."""
        ceiling = TRANSFER_MONEY_MAX if self.intent == "transfer" else MONEY_MAX
        if self.amount is not None and self.amount > ceiling:
            self.amount = None
        if self.destination_amount is not None and self.destination_amount > ceiling:
            self.destination_amount = None
        return self

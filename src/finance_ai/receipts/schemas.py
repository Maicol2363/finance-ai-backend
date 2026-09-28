"""Pydantic schemas and validation guardrails for HU-015 Receipt Photo Capture."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Canonical ISO-4217 currencies supported by the application
SUPPORTED_CURRENCIES = {
    "COP",
    "USD",
    "EUR",
    "MXN",
    "BRL",
    "CLP",
    "ARS",
    "PEN",
    "CAD",
    "GBP",
}


def _validate_finite_decimal(v: Decimal | None) -> Decimal | None:
    if v is not None and (math.isnan(v) or math.isinf(v)):
        raise ValueError("Amount must be a finite decimal number")
    return v


# ---------------------------------------------------------------------------
# Multimodal Provider Extraction DTOs (Strict Ingress Anti-Injection)
# ---------------------------------------------------------------------------


class RawCandidateItem(BaseModel):
    """Raw candidate line item parsed from multimodal provider output."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(default=Decimal("1.0"), ge=Decimal("0.01"), le=Decimal("99999.0"))
    unit_price: Decimal | None = Field(
        default=None, ge=Decimal("0.00"), le=Decimal("9999999999.99")
    )
    total_price: Decimal = Field(
        default=Decimal("0.00"), ge=Decimal("0.00"), le=Decimal("9999999999.99")
    )

    @field_validator("name", mode="after")
    @classmethod
    def clean_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Item name cannot be empty")
        return cleaned

    @field_validator("quantity", "unit_price", "total_price", mode="before")
    @classmethod
    def check_finite(cls, v: object) -> object:
        if isinstance(v, (Decimal, float, str)):
            try:
                dec = Decimal(str(v))
                if math.isnan(dec) or math.isinf(dec):
                    raise ValueError("Numeric value must be finite")
                return dec
            except Exception as e:
                if "finite" in str(e):
                    raise
        return v


class RawExtractionDTO(BaseModel):
    """Raw extraction DTO from multimodal LLM response with strict boundary defense."""

    model_config = ConfigDict(extra="forbid")

    is_receipt: bool = True
    merchant_name: str | None = Field(default=None, max_length=150)
    purchase_date: str | None = Field(default=None, description="ISO Date string YYYY-MM-DD")
    total_amount: Decimal | None = Field(
        default=None, ge=Decimal("0.00"), le=Decimal("9999999999.99")
    )
    currency: str = Field(default="COP", min_length=3, max_length=3)
    suggested_category: str | None = Field(default=None, max_length=100)
    suggested_account: str | None = Field(default=None, max_length=100)
    payment_method: str | None = Field(default=None, max_length=50)
    items: list[RawCandidateItem] = Field(default_factory=list, max_length=200)

    @field_validator("merchant_name", mode="after")
    @classmethod
    def clean_merchant_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = v.strip()
        return cleaned if cleaned else None

    @field_validator("purchase_date", mode="after")
    @classmethod
    def validate_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        try:
            d = date.fromisoformat(v[:10])
            return d.isoformat()
        except Exception:
            raise ValueError(f"Invalid calendar date: {v}") from None

    @field_validator("currency", mode="after")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        upper = v.strip().upper()
        if upper not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Unsupported or invalid currency code: {v}")
        return upper

    @field_validator("total_amount", mode="before")
    @classmethod
    def check_finite_money(cls, v: object) -> object:
        if v is not None and isinstance(v, (Decimal, float, str)):
            try:
                dec = Decimal(str(v))
                if math.isnan(dec) or math.isinf(dec):
                    raise ValueError("Monetary amount must be finite")
                return dec
            except Exception as e:
                if "finite" in str(e):
                    raise
        return v


# ---------------------------------------------------------------------------
# Domain Line Item & Detailed Receipt Extraction
# ---------------------------------------------------------------------------


class ReceiptItem(BaseModel):
    """Line item candidate extracted from a receipt."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(default=Decimal("1.0"), ge=Decimal("0.01"), le=Decimal("99999.0"))
    unit_price: Decimal | None = Field(
        default=None, ge=Decimal("0.00"), le=Decimal("9999999999.99")
    )
    amount: Decimal = Field(
        default=Decimal("0.00"), ge=Decimal("0.00"), le=Decimal("9999999999.99")
    )

    @field_validator("name", mode="after")
    @classmethod
    def clean_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Item name cannot be empty")
        return cleaned

    @field_validator("quantity", "unit_price", "amount", mode="before")
    @classmethod
    def check_finite(cls, v: object) -> object:
        if isinstance(v, (Decimal, float, str)):
            try:
                dec = Decimal(str(v))
                if math.isnan(dec) or math.isinf(dec):
                    raise ValueError("Numeric value must be finite")
                return dec
            except Exception as e:
                if "finite" in str(e):
                    raise
        return v


class ReceiptTaxes(BaseModel):
    """Fiscal taxes breakdown."""

    model_config = ConfigDict(extra="forbid")

    iva_19: Decimal | None = Field(default=None, ge=Decimal("0.00"))
    iva_5: Decimal | None = Field(default=None, ge=Decimal("0.00"))
    inc_8: Decimal | None = Field(default=None, ge=Decimal("0.00"))
    total_tax: Decimal | None = Field(default=None, ge=Decimal("0.00"))


class ReceiptExtraction(BaseModel):
    """Structured receipt extraction output with strict bounds and validation."""

    model_config = ConfigDict(extra="forbid")

    merchant: str = Field(min_length=1, max_length=150)
    occurred_at: str | None = Field(default=None, description="ISO Date string YYYY-MM-DD")
    printed_total: Decimal | None = Field(
        default=None, ge=Decimal("0.00"), le=Decimal("9999999999.99")
    )
    currency: str = Field(default="COP", min_length=3, max_length=3)
    payment_method: str | None = Field(default=None, max_length=80)
    suggested_category: str | None = Field(default=None, max_length=80)
    tip: Decimal | None = Field(default=None, ge=Decimal("0.00"), le=Decimal("9999999999.99"))
    taxes: ReceiptTaxes | None = None
    items: list[ReceiptItem] = Field(default_factory=list, max_length=200)

    # Reconciliation state (derived on server, never supplied by LLM)
    is_reconciled: bool = False
    reconciliation_notes: str | None = None
    is_receipt: bool = True
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, exclude=True)
    capture_source: str | None = Field(default=None, max_length=80, exclude=True)
    raw_cufe: str | None = Field(default=None, max_length=128, exclude=True)

    @field_validator("merchant", mode="after")
    @classmethod
    def clean_merchant(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Merchant name cannot be empty")
        return cleaned

    @field_validator("occurred_at", mode="after")
    @classmethod
    def validate_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        try:
            d = date.fromisoformat(v[:10])
            return d.isoformat()
        except Exception:
            raise ValueError(f"Invalid calendar date: {v}") from None

    @field_validator("currency", mode="after")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        upper = v.strip().upper()
        if upper not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Unsupported or invalid currency code: {v}")
        return upper

    @field_validator("printed_total", "tip", mode="before")
    @classmethod
    def check_finite_money(cls, v: object) -> object:
        if v is not None and isinstance(v, (Decimal, float, str)):
            try:
                dec = Decimal(str(v))
                if math.isnan(dec) or math.isinf(dec):
                    raise ValueError("Monetary amount must be finite")
                return dec
            except Exception as e:
                if "finite" in str(e):
                    raise
        return v

    @model_validator(mode="after")
    def reconcile_math(self) -> ReceiptExtraction:
        """Validates arithmetic consistency without fabricating missing totals."""
        if not self.items:
            self.is_reconciled = False
            self.reconciliation_notes = "No line items present to reconcile"
            return self

        items_sum = sum((item.amount for item in self.items), Decimal("0.00"))

        if self.printed_total is None:
            self.is_reconciled = False
            self.reconciliation_notes = "Printed total is absent; not computed"
            return self

        tip_amt = self.tip or Decimal("0.00")
        discrepancy = abs(items_sum - self.printed_total)
        discrepancy_with_tip = abs((items_sum + tip_amt) - self.printed_total)

        tolerance = max(Decimal("100.00"), self.printed_total * Decimal("0.02"))

        if discrepancy <= tolerance or discrepancy_with_tip <= tolerance:
            self.is_reconciled = True
            self.reconciliation_notes = "Item sum matches total within tolerance"
        else:
            self.is_reconciled = False
            self.reconciliation_notes = (
                f"Items sum ({items_sum}) differs from printed total ({self.printed_total})"
            )
        return self


# ---------------------------------------------------------------------------
# Proposal DTOs & Validation Issues
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    """Validation or sanitization finding on a proposal."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., max_length=64)
    message: str = Field(..., max_length=255)
    severity: Literal["info", "warning", "error"] = "warning"


class ReceiptProposalResponse(BaseModel):
    """Client-facing proposal response containing parsed candidates and issue hints."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    status: Literal[
        "complete", "partial", "not_a_receipt", "failed_extraction", "confirmed", "discarded"
    ]
    revision: int = 1
    merchant_name: str | None = None
    purchase_date: str | None = None
    total_amount: Decimal | None = None
    currency: str = "COP"
    payment_method: str | None = None
    suggested_category: str | None = None
    suggested_account: str | None = None
    items: list[ReceiptItem] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    possible_duplicate: bool = False
    duplicate_warning: str | None = None
    created_at: datetime
    expires_at: datetime


class ProposalRetryRequest(BaseModel):
    """Payload to retry or rotate receipt extraction."""

    model_config = ConfigDict(extra="forbid")

    rotation_degrees: Literal[0, 90, 180, 270] | None = 0


# ---------------------------------------------------------------------------
# Auxiliary Schemas for Optimization & Legacy Endpoints
# ---------------------------------------------------------------------------


class ImageOptimizationMeta(BaseModel):
    """Image processing and tile compression performance metadata."""

    model_config = ConfigDict(extra="forbid")

    original_bytes: int
    compressed_bytes: int
    savings_percent: float
    compression_time_ms: float
    width: int
    height: int
    tile_count: int


class ReceiptScanResponse(BaseModel):
    """Response payload for synchronous receipt scan endpoint."""

    model_config = ConfigDict(extra="forbid")

    extraction: ReceiptExtraction
    optimization: ImageOptimizationMeta
    ai_latency_ms: float
    total_latency_ms: float
    provider: str
    model: str


# ---------------------------------------------------------------------------
# Expense Confirmation Request Schemas (HU-015 Unit 2)
# ---------------------------------------------------------------------------


class ConfirmedReceiptItem(BaseModel):
    """User-confirmed line item for multi-item persistence in PostgreSQL."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    total_price: Decimal = Field(gt=Decimal("0.00"), le=Decimal("9999999999.99"))
    quantity: Decimal = Field(default=Decimal("1.0"), gt=Decimal("0.00"), le=Decimal("999999.999"))
    unit_price: Decimal | None = Field(
        default=None, ge=Decimal("0.00"), le=Decimal("9999999999.99")
    )
    category_id: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("name", mode="after")
    @classmethod
    def clean_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Item name cannot be empty")
        return cleaned


class ReceiptProposalConfirmRequest(BaseModel):
    """Payload to confirm an ephemeral receipt proposal into permanent financial records."""

    model_config = ConfigDict(extra="forbid")

    account_id: int = Field(ge=1)
    total_amount: Decimal = Field(gt=Decimal("0.00"), le=Decimal("9999999999.99"))
    merchant: str = Field(min_length=1, max_length=150)
    occurred_at: datetime
    currency: str = Field(min_length=3, max_length=3)
    category_id: int | None = Field(default=None, ge=1)
    new_category_name: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=500)
    items: list[ConfirmedReceiptItem] = Field(default_factory=list, max_length=200)

    @field_validator("merchant", mode="after")
    @classmethod
    def clean_merchant(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Merchant cannot be empty")
        return cleaned

    @field_validator("currency", mode="after")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        upper = v.strip().upper()
        if upper not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Unsupported or invalid currency code: {v}")
        return upper

"""Sanitization, prompt-injection defense, and arithmetic verification for receipt proposals."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from finance_ai.core.logging import get_logger
from finance_ai.receipts.schemas import (
    RawExtractionDTO,
    ReceiptItem,
    ReceiptProposalResponse,
    ValidationIssue,
)

logger = get_logger(__name__)

# Heuristic patterns detecting embedded script tags, code declarations, SQL, and jailbreak commands
SUSPICIOUS_CODE_REGEX = re.compile(
    r"""(?xi)
    <\s*script\b[^>]*>|
    <\s*/\s*script\s*>|
    javascript:|
    \b(SELECT\s+.*FROM|INSERT\s+INTO|DROP\s+TABLE|UPDATE\s+.*SET|UNION\s+SELECT|ALTER\s+TABLE)\b|
    \bdef\s+[a-zA-Z_]\w*\s*\(|
    \bclass\s+[a-zA-Z_]\w*[:\(]|
    \bimport\s+[a-zA-Z_]|
    \bfrom\s+[a-zA-Z_]\w*\s+import|
    \b(bash\s+-c|curl\s+http|wget\s+http|rm\s+-rf|powershell|eval\(|exec\()|
    ```|~~~|
    \b(ignore\s+previous|system\s+prompt|disregard\s+all|override\s+instructions)\b
    """
)


def contains_suspicious_payload(text: str | None) -> bool:
    """Checks whether a string contains code fragments or prompt injection indicators."""
    if not text:
        return False
    return bool(SUSPICIOUS_CODE_REGEX.search(text))


def sanitize_extraction(
    dto: RawExtractionDTO,
    proposal_id: UUID | None = None,
    revision: int = 1,
) -> ReceiptProposalResponse:
    """Transforms raw LLM extraction into a verified, sanitized domain proposal."""
    pid = proposal_id or uuid4()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=24)
    issues: list[ValidationIssue] = []

    # 1. Non-Receipt Gate
    if not dto.is_receipt:
        issues.append(
            ValidationIssue(
                code="IMAGE_NOT_A_RECEIPT",
                message="La imagen no parece ser un recibo de compra válido.",
                severity="error",
            )
        )
        return ReceiptProposalResponse(
            proposal_id=pid,
            status="not_a_receipt",
            revision=revision,
            merchant_name=None,
            purchase_date=None,
            total_amount=None,
            currency="COP",
            items=[],
            issues=issues,
            created_at=now,
            expires_at=expires_at,
        )

    # 2. Anti-Code & Schema Hijacking Heuristics on merchant name
    merchant = dto.merchant_name
    if merchant and contains_suspicious_payload(merchant):
        logger.warning(
            "Sanitizer stripped suspicious code/command in merchant_name: %s", merchant[:60]
        )
        merchant = None
        issues.append(
            ValidationIssue(
                code="SUSPICIOUS_PAYLOAD_FILTERED",
                message="Se filtró texto con sintaxis no permitida en el nombre del comercio.",
                severity="warning",
            )
        )

    # 3. Anti-Code Heuristics on item names
    sanitized_items: list[ReceiptItem] = []
    for item in dto.items:
        clean_name = item.name
        if contains_suspicious_payload(clean_name):
            logger.warning("Sanitizer detected suspicious code in item name: %s", clean_name[:60])
            issues.append(
                ValidationIssue(
                    code="SUSPICIOUS_PAYLOAD_FILTERED",
                    message="Se filtró texto sospechoso en la descripción de un producto.",
                    severity="warning",
                )
            )
            continue

        sanitized_items.append(
            ReceiptItem(
                name=clean_name,
                quantity=item.quantity,
                unit_price=item.unit_price,
                amount=item.total_price,
            )
        )

    # Financial Plausibility Gate: zero monetary evidence
    total_amt = dto.total_amount
    if total_amt is None and not sanitized_items:
        issues.append(
            ValidationIssue(
                code="IMAGE_NOT_A_RECEIPT",
                message="No se encontró información financiera reconocible en el comprobante.",
                severity="error",
            )
        )
        return ReceiptProposalResponse(
            proposal_id=pid,
            status="not_a_receipt",
            revision=revision,
            merchant_name=None,
            purchase_date=None,
            total_amount=None,
            currency=dto.currency,
            items=[],
            issues=issues,
            created_at=now,
            expires_at=expires_at,
        )

    # 4. Arithmetic Integrity Verification
    if total_amt is not None and sanitized_items:
        items_sum = sum((item.amount for item in sanitized_items), Decimal("0.00"))
        discrepancy = abs(items_sum - total_amt)
        tolerance = (
            max(Decimal("100.00"), total_amt * Decimal("0.02"))
            if dto.currency == "COP"
            else Decimal("0.05")
        )
        if discrepancy > tolerance:
            issues.append(
                ValidationIssue(
                    code="ARITHMETIC_MISMATCH",
                    message=(
                        f"La suma de los productos ({items_sum}) no coincide exactamente "
                        f"con el total ({total_amt})."
                    ),
                    severity="warning",
                )
            )
    elif total_amt is None:
        issues.append(
            ValidationIssue(
                code="TOTAL_NOT_DETECTED",
                message="No se detectó un total impreso claro. Por favor verifica el monto.",
                severity="info",
            )
        )

    # 5. Determine Proposal Status
    status = "complete" if (merchant is not None and total_amt is not None) else "partial"

    return ReceiptProposalResponse(
        proposal_id=pid,
        status=status,
        revision=revision,
        merchant_name=merchant,
        purchase_date=dto.purchase_date,
        total_amount=total_amt,
        currency=dto.currency,
        payment_method=dto.payment_method,
        suggested_category=dto.suggested_category,
        suggested_account=dto.suggested_account,
        items=sanitized_items,
        issues=issues,
        created_at=now,
        expires_at=expires_at,
    )


def sanitize_raw_extraction(
    data: RawExtractionDTO | dict[str, Any],
    proposal_id: UUID | None = None,
    revision: int = 1,
) -> ReceiptProposalResponse:
    dto = RawExtractionDTO.model_validate(data) if isinstance(data, dict) else data
    return sanitize_extraction(dto, proposal_id=proposal_id, revision=revision)


sanitize = sanitize_raw_extraction


PROVIDER_UNAVAILABLE_MESSAGE = (
    "No se pudo extraer la información automáticamente. Puedes ingresar los datos manualmente."
)


def create_fallback_proposal(
    proposal_id: UUID | None = None,
    revision: int = 1,
) -> ReceiptProposalResponse:
    """Generates an editable fallback proposal when upstream AI providers fail or timeout.

    The issue text is a fixed, safe message on purpose: provider failures carry internal request
    identifiers, quota details and request echoes that must never reach the client (AGENTS.md
    error and privacy contract).
    """
    pid = proposal_id or uuid4()
    now = datetime.now(UTC)
    # Same 24h window as a normal draft: filling the required fields by hand after a provider
    # outage must not race the clock.
    expires_at = now + timedelta(hours=24)

    return ReceiptProposalResponse(
        proposal_id=pid,
        status="failed_extraction",
        revision=revision,
        merchant_name=None,
        purchase_date=None,
        total_amount=None,
        currency="COP",
        items=[],
        issues=[
            ValidationIssue(
                code="PROVIDER_UNAVAILABLE",
                message=PROVIDER_UNAVAILABLE_MESSAGE,
                severity="error",
            )
        ],
        created_at=now,
        expires_at=expires_at,
    )

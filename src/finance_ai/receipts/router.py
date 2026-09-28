"""FastAPI router for receipt proposal capture, rotation retries, and scanning."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from finance_ai.core.config import get_settings
from finance_ai.core.logging import get_logger
from finance_ai.db.session import get_db_session
from finance_ai.expenses.dependencies import get_expense_service
from finance_ai.expenses.schemas import ExpenseResponse
from finance_ai.expenses.service import ExpenseService
from finance_ai.receipts.deepseek_client import DeepSeekVisionService
from finance_ai.receipts.dependencies import get_current_user_id
from finance_ai.receipts.dian_resolver import DianResolverService
from finance_ai.receipts.gateway import ReceiptVisionGateway, get_vision_gateway
from finance_ai.receipts.guards import (
    get_receipt_rate_limiter,
    read_and_validate_stream,
    validate_image_magic_header,
)
from finance_ai.receipts.preprocessor import (
    preprocess_receipt_image,
    preprocess_receipt_image_ephemeral,
)
from finance_ai.receipts.repository import ReceiptProposalRepository
from finance_ai.receipts.sanitizer import create_fallback_proposal, sanitize_extraction
from finance_ai.receipts.schemas import (
    DianBypassRequest,
    ReceiptProposalConfirmRequest,
    ReceiptProposalResponse,
    ReceiptScanResponse,
    ValidationIssue,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/receipts", tags=["Receipt Intelligence"])


def _check_and_mark_duplicate(
    proposal: ReceiptProposalResponse,
    user_id: int,
    expense_service: ExpenseService,
) -> ReceiptProposalResponse:
    try:
        from datetime import UTC, datetime

        if not proposal.merchant_name or not proposal.total_amount:
            return proposal
        if proposal.purchase_date:
            try:
                p_date = datetime.strptime(proposal.purchase_date, "%Y-%m-%d").date()
            except Exception:
                p_date = datetime.now(UTC).date()
        else:
            p_date = datetime.now(UTC).date()

        if expense_service.find_potential_duplicate(
            owner_id=user_id,
            merchant=proposal.merchant_name,
            total_amount=proposal.total_amount,
            occurred_date=p_date,
        ):
            warning_msg = (
                f"Ya registraste una compra en {proposal.merchant_name} "
                f"por ${proposal.total_amount:,.2f} en esta fecha."
            )
            return proposal.model_copy(
                update={
                    "possible_duplicate": True,
                    "duplicate_warning": warning_msg,
                    "issues": [
                        *proposal.issues,
                        ValidationIssue(
                            code="possible_duplicate",
                            message=warning_msg,
                            severity="warning",
                        ),
                    ],
                }
            )
    except Exception as exc:
        logger.warning(
            "Duplicate pre-check skipped for a receipt proposal",
            extra={
                "event": "receipt.duplicate_check.failed",
                "outcome": "degraded",
                "error_type": type(exc).__name__,
            },
        )
    return proposal


def _load_prompt_context(owner_id: int) -> tuple[list[str] | None, list[str] | None]:
    """Loads the caller's category and account names used to steer the extraction prompt."""
    try:
        from finance_ai.categories.dependencies import get_category_service

        category_service = get_category_service()
        cat_result = category_service.list(owner_id=owner_id)
        existing_categories: list[str] | None = [c.name for c in cat_result.categories]
    except Exception as exc:
        logger.warning(
            "Category context unavailable for the receipt prompt",
            extra={
                "event": "receipt.prompt_context.failed",
                "outcome": "degraded",
                "error_type": type(exc).__name__,
            },
        )
        existing_categories = None

    try:
        from finance_ai.accounts.dependencies import get_account_service

        account_service = get_account_service()
        acc_result = account_service.list(
            owner_id=owner_id, preferred_currency_code="COP", include_inactive=False
        )
        existing_accounts: list[str] | None = [a.name for a in acc_result.accounts]
    except Exception as exc:
        logger.warning(
            "Account context unavailable for the receipt prompt",
            extra={
                "event": "receipt.prompt_context.failed",
                "outcome": "degraded",
                "error_type": type(exc).__name__,
            },
        )
        existing_accounts = None

    return existing_categories, existing_accounts


def _fallback_after_extraction_failure(
    exc: Exception,
    *,
    proposal_id: UUID | None = None,
    revision: int = 1,
) -> ReceiptProposalResponse:
    """Returns the manual-entry draft for a failed extraction.

    Only the failure type is logged: upstream exceptions can embed provider response bodies and
    request URLs, and the client-visible issue text stays a fixed safe message (AGENTS.md).
    """
    logger.warning(
        "Receipt extraction failed; returning a manual-entry draft",
        extra={
            "event": "receipt.extraction.failed",
            "outcome": "fallback",
            "error_type": type(exc).__name__,
        },
    )
    return create_fallback_proposal(proposal_id=proposal_id, revision=revision)


async def _build_proposal_draft(
    *,
    image_bytes: bytes,
    owner_id: int,
    gateway: ReceiptVisionGateway,
    proposal_id: UUID | None = None,
    revision: int = 1,
) -> ReceiptProposalResponse:
    """Extracts and sanitizes a receipt proposal, degrading gracefully to manual entry."""
    existing_categories, existing_accounts = _load_prompt_context(owner_id)
    try:
        dto, _ = await gateway.extract_receipt_dto(
            image_bytes,
            existing_categories=existing_categories,
            existing_accounts=existing_accounts,
        )
    except Exception as exc:
        return _fallback_after_extraction_failure(exc, proposal_id=proposal_id, revision=revision)
    return sanitize_extraction(dto, proposal_id=proposal_id, revision=revision)


@router.post(
    "/proposal",
    response_model=ReceiptProposalResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload receipt photo and generate fast draft proposal",
)
async def create_receipt_proposal(
    file: Annotated[UploadFile, File(...)],
    user_id: Annotated[int, Depends(get_current_user_id)],
    db_session: Annotated[Session, Depends(get_db_session)],
    expense_service: Annotated[ExpenseService, Depends(get_expense_service)],
    rotation: Annotated[int, Query()] = 0,
    rotation_degrees: Annotated[int | None, Form()] = None,
) -> ReceiptProposalResponse:
    """Processes uploaded receipt image strictly in-memory and creates a structured draft."""
    # 1. Rate Limiting Check
    rate_limiter = get_receipt_rate_limiter()
    rate_limiter.check_rate_limit(user_id)

    # 2. Ingress Bounded Reading (10 MiB limit)
    raw_bytes = await read_and_validate_stream(file)

    effective_rotation = rotation_degrees if rotation_degrees is not None else rotation

    try:
        # 3. Magic Header Sniffing
        validate_image_magic_header(raw_bytes[:32])

        # 4. Ephemeral Preprocessing (rotation, metadata strip, Q85 JPEG)
        comp_bytes, _ = await preprocess_receipt_image_ephemeral(
            raw_bytes,
            rotation_degrees=effective_rotation,
            quality=85,
        )
    finally:
        del raw_bytes

    # 5. Multimodal Vision Extraction with Fallback Graceful Degradation
    settings = get_settings()
    gateway = get_vision_gateway(settings)

    try:
        proposal = await _build_proposal_draft(
            image_bytes=comp_bytes,
            owner_id=user_id,
            gateway=gateway,
        )
    finally:
        del comp_bytes

    # 6. Check potential duplicate
    proposal = _check_and_mark_duplicate(proposal, user_id, expense_service)

    # 7. Save Draft State in Database (0 Media Blob Bytes)
    repo = ReceiptProposalRepository(db_session)
    repo.create(user_id, proposal)
    db_session.commit()

    return proposal


@router.get(
    "/proposal/{proposal_id}",
    response_model=ReceiptProposalResponse,
    status_code=status.HTTP_200_OK,
    summary="Fetch existing receipt proposal draft",
)
def get_receipt_proposal(
    proposal_id: UUID,
    user_id: Annotated[int, Depends(get_current_user_id)],
    db_session: Annotated[Session, Depends(get_db_session)],
) -> ReceiptProposalResponse:
    """Retrieves an existing proposal ensuring user ownership."""
    repo = ReceiptProposalRepository(db_session)
    row = repo.get_by_id_for_user(user_id, proposal_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Receipt proposal not found.",
        )
    return ReceiptProposalResponse.model_validate(row.proposal_data)


@router.post(
    "/proposal/{proposal_id}/retry",
    response_model=ReceiptProposalResponse,
    status_code=status.HTTP_200_OK,
    summary="Client-driven retry or rotation for a proposal",
)
async def retry_receipt_proposal(
    proposal_id: UUID,
    file: Annotated[UploadFile, File(...)],
    user_id: Annotated[int, Depends(get_current_user_id)],
    db_session: Annotated[Session, Depends(get_db_session)],
    expense_service: Annotated[ExpenseService, Depends(get_expense_service)],
    rotation: Annotated[int, Query()] = 0,
    rotation_degrees: Annotated[int | None, Form()] = None,
) -> ReceiptProposalResponse:
    """Re-analyzes receipt with updated rotation orientation without persistent cloud storage."""
    repo = ReceiptProposalRepository(db_session)
    row = repo.get_by_id_for_user(user_id, proposal_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Receipt proposal not found.",
        )

    rate_limiter = get_receipt_rate_limiter()
    rate_limiter.check_rate_limit(user_id)

    raw_bytes = await read_and_validate_stream(file)
    effective_rotation = rotation_degrees if rotation_degrees is not None else rotation
    try:
        validate_image_magic_header(raw_bytes[:32])
        comp_bytes, _ = await preprocess_receipt_image_ephemeral(
            raw_bytes,
            rotation_degrees=effective_rotation,
            quality=85,
        )
    finally:
        del raw_bytes

    settings = get_settings()
    gateway = get_vision_gateway(settings)

    try:
        new_proposal = await _build_proposal_draft(
            image_bytes=comp_bytes,
            owner_id=user_id,
            gateway=gateway,
            proposal_id=proposal_id,
            revision=row.revision + 1,
        )
    finally:
        del comp_bytes

    new_proposal = _check_and_mark_duplicate(new_proposal, user_id, expense_service)
    repo.update(user_id, proposal_id, new_proposal)
    db_session.commit()

    return new_proposal


@router.delete(
    "/proposal/{proposal_id}",
    status_code=status.HTTP_200_OK,
    summary="Discard receipt proposal draft",
)
def discard_receipt_proposal(
    proposal_id: UUID,
    user_id: Annotated[int, Depends(get_current_user_id)],
    db_session: Annotated[Session, Depends(get_db_session)],
) -> dict[str, Any]:
    """Marks a proposal draft as discarded."""
    repo = ReceiptProposalRepository(db_session)
    discarded = repo.discard(user_id, proposal_id)
    if not discarded:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Receipt proposal not found.",
        )
    db_session.commit()
    return {"status": "discarded", "proposal_id": str(proposal_id)}


@router.post(
    "/proposal/{proposal_id}/confirm",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm receipt proposal into permanent financial records",
)
def confirm_receipt_proposal(
    proposal_id: UUID,
    payload: ReceiptProposalConfirmRequest,
    response: Response,
    user_id: Annotated[int, Depends(get_current_user_id)],
    expense_service: Annotated[ExpenseService, Depends(get_expense_service)],
) -> ExpenseResponse:
    """Validates proposal, checks account/currency, and executes confirmation in PostgreSQL."""
    record, is_new = expense_service.confirm_receipt_proposal(
        owner_id=user_id,
        proposal_id=proposal_id,
        command=payload,
    )
    if not is_new:
        response.status_code = status.HTTP_200_OK
    return ExpenseResponse.from_record(record)


# ---------------------------------------------------------------------------
# Legacy Endpoints for Compatibility
# ---------------------------------------------------------------------------


@router.post(
    "/scan",
    response_model=ReceiptScanResponse,
    status_code=status.HTTP_200_OK,
    summary="Legacy high-speed receipt photo scan",
)
async def scan_receipt(
    file: Annotated[UploadFile, File(...)],
    user_id: Annotated[int, Depends(get_current_user_id)],
) -> ReceiptScanResponse:
    """Clamps to 768px tile boundary, compresses to JPEG Q75, and queries DeepSeek Vision."""
    settings = get_settings()

    rate_limiter = get_receipt_rate_limiter()
    rate_limiter.check_rate_limit(user_id)

    raw_bytes = await read_and_validate_stream(file)
    try:
        validate_image_magic_header(raw_bytes[:32])
        comp_bytes, opt_meta = preprocess_receipt_image(raw_bytes, clamp_width=768, quality=75)
    finally:
        del raw_bytes

    vision_service = DeepSeekVisionService(settings)
    try:
        extraction, ai_ms = await vision_service.extract_receipt(comp_bytes)
    except Exception as exc:
        logger.error(
            "Legacy receipt scan extraction failed",
            extra={
                "event": "receipt.scan.failed",
                "outcome": "upstream_error",
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Receipt extraction failed.",
        ) from exc

    total_ms = round(opt_meta.compression_time_ms + ai_ms, 2)

    return ReceiptScanResponse(
        extraction=extraction,
        optimization=opt_meta,
        ai_latency_ms=ai_ms,
        total_latency_ms=total_ms,
        provider="deepseek",
        model=settings.deepseek_model,
    )


@router.post(
    "/dian-bypass",
    status_code=status.HTTP_200_OK,
    summary="Tier-0 Fast Path: DIAN electronic invoice resolution via CUFE",
)
async def resolve_dian_cufe(
    request: DianBypassRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
) -> dict[str, Any]:
    """Resolves certified Colombian electronic invoice via DIAN in < 300ms ($0.00 AI cost)."""
    dian_service = DianResolverService()
    extraction, latency_ms, status_msg = await dian_service.resolve_cufe(request.cufe)

    if not extraction:
        return {
            "status": "FALLBACK_TO_VISION",
            "reason": status_msg,
            "latency_ms": latency_ms,
        }

    return {
        "status": "SUCCESS",
        "extraction": extraction.model_dump(mode="json"),
        "latency_ms": latency_ms,
        "provider": "dian_gov_co",
    }

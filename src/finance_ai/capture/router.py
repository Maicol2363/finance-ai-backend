"""Authenticated HTTP routes for the HU-013 text capture."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from finance_ai.accounts.dependencies import get_authenticated_principal
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.capture.dependencies import (
    enforce_text_capture_rate_limit,
    get_text_capture_service,
)
from finance_ai.capture.schemas import (
    TextCaptureConfirmation,
    TextProposalConfirmRequest,
    TextProposalRequest,
    TextProposalResponse,
)
from finance_ai.capture.service import TextCaptureService
from finance_ai.core.errors import ProblemResponse

router = APIRouter(prefix="/capture/text", tags=["Text Capture"])

CAPTURE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ProblemResponse} for code in (401, 404, 409, 413, 415, 422, 429, 500, 503)
}


@router.post(
    "/proposals",
    response_model=TextProposalResponse,
    status_code=status.HTTP_201_CREATED,
    responses=CAPTURE_ERROR_RESPONSES,
    summary="Propose a financial operation from natural text (nothing is saved)",
    dependencies=[Depends(enforce_text_capture_rate_limit)],
)
async def create_text_proposal(
    payload: TextProposalRequest,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[TextCaptureService, Depends(get_text_capture_service)],
) -> TextProposalResponse:
    """A first message, a typed correction or a tapped choice (corrections keep the text)."""
    return await service.propose(owner_id=principal.user_id, request=payload)


@router.get(
    "/proposals/{proposal_id}",
    response_model=TextProposalResponse,
    responses=CAPTURE_ERROR_RESPONSES,
)
def get_text_proposal(
    proposal_id: UUID,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[TextCaptureService, Depends(get_text_capture_service)],
) -> TextProposalResponse:
    return service.get(owner_id=principal.user_id, proposal_id=proposal_id)


@router.delete(
    "/proposals/{proposal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=CAPTURE_ERROR_RESPONSES,
)
def discard_text_proposal(
    proposal_id: UUID,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[TextCaptureService, Depends(get_text_capture_service)],
) -> Response:
    service.discard(owner_id=principal.user_id, proposal_id=proposal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/proposals/{proposal_id}/confirm",
    response_model=TextCaptureConfirmation,
    status_code=status.HTTP_201_CREATED,
    responses=CAPTURE_ERROR_RESPONSES,
    summary="Record the proposal through the normal validated operation",
)
def confirm_text_proposal(
    proposal_id: UUID,
    payload: TextProposalConfirmRequest,
    response: Response,
    principal: Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)],
    service: Annotated[TextCaptureService, Depends(get_text_capture_service)],
) -> TextCaptureConfirmation:
    confirmation = service.confirm(
        owner_id=principal.user_id, proposal_id=proposal_id, command=payload
    )
    if confirmation.replayed:
        response.status_code = status.HTTP_200_OK
    return confirmation

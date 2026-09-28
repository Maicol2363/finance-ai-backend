"""FastAPI dependencies for receipt scanning and proposal management."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from finance_ai.auth.dependencies import get_session_service, require_bearer_access
from finance_ai.auth.session_service import SessionService


def get_current_user_id(
    access_credential: Annotated[str, Depends(require_bearer_access)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> int:
    """Authenticates request via Bearer token and returns integer user_id."""
    return session_service.current_user(access_credential).user_id

"""The signed-in person's email notification settings (EML-02)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from finance_ai.accounts.dependencies import get_authenticated_principal
from finance_ai.auth.session_schemas import PublicAuthenticatedUser
from finance_ai.core.errors import ProblemResponse
from finance_ai.notifications.account import NotificationAccountService
from finance_ai.notifications.dependencies import get_notification_account_service
from finance_ai.notifications.schemas import (
    PreferencesResponse,
    PreferencesUpdateRequest,
    TestEmailResponse,
    VerificationIssuedResponse,
    VerifyEmailRequest,
)

router = APIRouter(prefix="/me", tags=["Notifications"])

Principal = Annotated[PublicAuthenticatedUser, Depends(get_authenticated_principal)]
Service = Annotated[NotificationAccountService, Depends(get_notification_account_service)]

ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ProblemResponse},
    401: {"model": ProblemResponse},
    404: {"model": ProblemResponse},
    409: {"model": ProblemResponse},
    415: {"model": ProblemResponse},
    422: {"model": ProblemResponse},
    429: {"model": ProblemResponse},
    503: {"model": ProblemResponse},
}


@router.get("/notification-preferences", response_model=PreferencesResponse, responses=ERRORS)
def get_preferences(principal: Principal, service: Service) -> PreferencesResponse:
    return PreferencesResponse.from_view(service.get_preferences(principal.user_id))


@router.patch("/notification-preferences", response_model=PreferencesResponse, responses=ERRORS)
def update_preferences(
    payload: PreferencesUpdateRequest, principal: Principal, service: Service
) -> PreferencesResponse:
    return PreferencesResponse.from_view(
        service.update_preferences(
            principal.user_id,
            email_enabled=payload.email_enabled,
            budget_alerts=payload.budget_alerts,
            show_amounts=payload.show_amounts,
        )
    )


@router.post("/email/verification", response_model=VerificationIssuedResponse, responses=ERRORS)
def request_email_verification(
    principal: Principal, service: Service
) -> VerificationIssuedResponse:
    return VerificationIssuedResponse.from_result(service.request_verification(principal.user_id))


@router.post("/email/verify", response_model=PreferencesResponse, responses=ERRORS)
def verify_email(
    payload: VerifyEmailRequest, principal: Principal, service: Service
) -> PreferencesResponse:
    return PreferencesResponse.from_view(
        service.confirm_verification(principal.user_id, payload.code)
    )


@router.post("/notifications/test", response_model=TestEmailResponse, responses=ERRORS)
def send_test_email(principal: Principal, service: Service) -> TestEmailResponse:
    return TestEmailResponse.from_result(service.send_test_email(principal.user_id))

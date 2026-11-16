"""Request and response bodies for the account-facing notification endpoints."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from finance_ai.notifications.account import PreferencesView, TestEmailResult, VerificationIssued


class PreferencesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_hint: str = Field(description="The account address with the middle hidden.")
    email_verified: bool
    email_enabled: bool
    budget_alerts: bool
    show_amounts: bool
    delivery_available: bool = Field(
        description="False when this deployment cannot send email; the app should hide the option."
    )

    @classmethod
    def from_view(cls, view: PreferencesView) -> Self:
        return cls(
            email_hint=view.email_hint,
            email_verified=view.email_verified,
            email_enabled=view.email_enabled,
            budget_alerts=view.budget_alerts,
            show_amounts=view.show_amounts,
            delivery_available=view.delivery_available,
        )


class PreferencesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Strict: only JSON true/false, never "yes" or 1 coerced into a consent decision.
    email_enabled: StrictBool | None = None
    budget_alerts: StrictBool | None = None
    show_amounts: StrictBool | None = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Send at least one preference to change")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("A preference cannot be null")
        return self


class VerificationIssuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_hint: str
    expires_in_seconds: int
    resend_in_seconds: int

    @classmethod
    def from_result(cls, result: VerificationIssued) -> Self:
        return cls(
            email_hint=result.email_hint,
            expires_in_seconds=result.expires_in_seconds,
            resend_in_seconds=result.resend_in_seconds,
        )


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[0-9]{6}$", description="The six digits from the email.")


class TestEmailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["sent", "queued"]

    @classmethod
    def from_result(cls, result: TestEmailResult) -> Self:
        return cls(status=result.status)

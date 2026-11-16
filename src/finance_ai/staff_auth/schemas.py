from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class StaffLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128, json_schema_extra={"writeOnly": True})


class StaffLoginCommand:
    def __init__(self, *, email: str, password: str) -> None:
        self.email = email
        self.password = password


class StaffSessionUser(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    email: str
    role: str


class StaffSessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: StaffSessionUser
    access_expires_at: datetime
    renewable_expires_at: datetime
    absolute_expires_at: datetime


class StaffMfaChallengeView(BaseModel):
    """Password accepted; the account must complete TOTP before any staff authority."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["mfa_required"] = "mfa_required"
    enrollment_required: bool
    challenge_expires_at: datetime


class StaffMfaEnrollmentView(BaseModel):
    """Exposed only inside a valid enrollment challenge; never logged or stored in clear."""

    model_config = ConfigDict(extra="forbid")

    issuer: str
    account_name: str
    secret: str
    otpauth_uri: str
    qr_svg_data_url: str


class StaffMfaResetRequest(BaseModel):
    """Lost-device recovery or voluntary authenticator replacement (exactly one proof)."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128, json_schema_extra={"writeOnly": True})
    recovery_code: SecretStr | None = Field(
        default=None, min_length=16, max_length=32, json_schema_extra={"writeOnly": True}
    )
    code: SecretStr | None = Field(
        default=None, min_length=6, max_length=6, json_schema_extra={"writeOnly": True}
    )


class StaffRecoveryCodesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=1, max_length=128, json_schema_extra={"writeOnly": True})


class StaffRecoveryCodesView(BaseModel):
    """Single-use codes shown once; only their hashes are stored."""

    model_config = ConfigDict(extra="forbid")

    recovery_codes: list[str]


class StaffMfaVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Six digits are enforced by the verifier; the value is never echoed in errors or logs.
    code: SecretStr = Field(min_length=6, max_length=6, json_schema_extra={"writeOnly": True})


class StaffActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr = Field(min_length=1, max_length=64, json_schema_extra={"writeOnly": True})
    password: SecretStr = Field(min_length=1, max_length=128, json_schema_extra={"writeOnly": True})
    password_confirmation: SecretStr = Field(
        min_length=1, max_length=128, json_schema_extra={"writeOnly": True}
    )

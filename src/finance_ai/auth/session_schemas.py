from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


@dataclass(frozen=True)
class LoginCommand:
    email: str
    password: str
    platform: Literal["android", "windows"]


@dataclass(frozen=True)
class PublicAuthenticatedUser:
    user_id: int
    email: str
    preferred_currency: str


@dataclass(frozen=True)
class IssuedSession:
    user: PublicAuthenticatedUser
    access_credential: str
    renewable_credential: str
    access_expires_at: datetime
    renewable_expires_at: datetime
    absolute_expires_at: datetime


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )
    platform: Literal["android", "windows"]

    def to_command(self) -> LoginCommand:
        return LoginCommand(
            email=self.email,
            password=self.password.get_secret_value(),
            platform=self.platform,
        )


class RenewableCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    renewable_credential: SecretStr = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )

    def raw_credential(self) -> str:
        return self.renewable_credential.get_secret_value()


class AuthenticatedUserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    email: str
    preferred_currency: str

    @classmethod
    def from_result(cls, user: PublicAuthenticatedUser) -> AuthenticatedUserResponse:
        return cls(
            id=user.user_id,
            email=user.email,
            preferred_currency=user.preferred_currency,
        )


class IssuedSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: AuthenticatedUserResponse
    token_type: Literal["Bearer"] = "Bearer"
    access_credential: str
    renewable_credential: str
    access_expires_at: datetime
    renewable_expires_at: datetime
    absolute_expires_at: datetime

    @classmethod
    def from_result(cls, result: IssuedSession) -> IssuedSessionResponse:
        return cls(
            user=AuthenticatedUserResponse.from_result(result.user),
            access_credential=result.access_credential,
            renewable_credential=result.renewable_credential,
            access_expires_at=result.access_expires_at,
            renewable_expires_at=result.renewable_expires_at,
            absolute_expires_at=result.absolute_expires_at,
        )

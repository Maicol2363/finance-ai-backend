from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, SecretStr


@dataclass(frozen=True)
class RegisterCommand:
    email: str
    password: str
    password_confirmation: str


@dataclass(frozen=True)
class ValidatedRegistration:
    normalized_email: str
    password: str


@dataclass(frozen=True)
class RegistrationResult:
    user_id: int
    normalized_email: str
    preferred_currency: str
    message: str = "Account created successfully. You can now log in."


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )
    password_confirmation: SecretStr = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )

    def to_command(self) -> RegisterCommand:
        return RegisterCommand(
            email=self.email,
            password=self.password.get_secret_value(),
            password_confirmation=self.password_confirmation.get_secret_value(),
        )


class RegisteredUserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    email: str
    preferred_currency: str


class RegistrationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: RegisteredUserResponse
    message: str

    @classmethod
    def from_result(cls, result: RegistrationResult) -> RegistrationResponse:
        return cls(
            user=RegisteredUserResponse(
                id=result.user_id,
                email=result.normalized_email,
                preferred_currency=result.preferred_currency,
            ),
            message=result.message,
        )

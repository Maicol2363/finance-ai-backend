from datetime import datetime

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

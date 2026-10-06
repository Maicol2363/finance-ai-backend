from pydantic import BaseModel, ConfigDict

from finance_ai.income_sources.repositories import IncomeSourceRecord


class IncomeSourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str

    @classmethod
    def from_record(cls, record: IncomeSourceRecord) -> IncomeSourceResponse:
        return cls(id=record.id, name=record.name)


class IncomeSourceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[IncomeSourceResponse]


class CreateIncomeSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class RenameIncomeSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

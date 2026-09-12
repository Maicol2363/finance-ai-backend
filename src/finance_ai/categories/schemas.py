from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema


class CategoryColor(StrEnum):
    BLUE = "blue"
    GREEN = "green"
    ORANGE = "orange"
    PURPLE = "purple"
    PINK = "pink"
    RED = "red"
    TEAL = "teal"
    YELLOW = "yellow"
    GRAY = "gray"


class CategoryIcon(StrEnum):
    ESSENTIALS = "essentials"
    WANTS = "wants"
    FOOD = "food"
    TRANSPORT = "transport"
    HOME = "home"
    HEALTH = "health"
    EDUCATION = "education"
    ENTERTAINMENT = "entertainment"
    SHOPPING = "shopping"
    BILLS = "bills"
    TRAVEL = "travel"
    GIFT = "gift"
    OTHER = "other"


@dataclass(frozen=True)
class CategoryView:
    id: int
    name: str
    color: CategoryColor
    icon: CategoryIcon
    sort_order: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CategoryListResult:
    categories: tuple[CategoryView, ...]
    supported_colors: tuple[CategoryColor, ...]
    supported_icons: tuple[CategoryIcon, ...]


@dataclass(frozen=True)
class CategoryCreateCommand:
    name: str
    color: CategoryColor
    icon: CategoryIcon


@dataclass(frozen=True)
class CategoryUpdateCommand:
    name: str | None = None
    color: CategoryColor | None = None
    icon: CategoryIcon | None = None


class CategoryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    color: CategoryColor = CategoryColor.BLUE
    icon: CategoryIcon = CategoryIcon.OTHER

    def to_command(self) -> CategoryCreateCommand:
        return CategoryCreateCommand(name=self.name, color=self.color, icon=self.icon)


class CategoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100)] | SkipJsonSchema[None] = None
    color: CategoryColor | SkipJsonSchema[None] = None
    icon: CategoryIcon | SkipJsonSchema[None] = None

    @field_validator("name", "color", "icon", mode="after")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field cannot be null")
        return value

    @model_validator(mode="after")
    def require_field(self) -> CategoryUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self

    def to_command(self) -> CategoryUpdateCommand:
        fields = self.model_fields_set
        return CategoryUpdateCommand(
            name=self.name if "name" in fields else None,
            color=self.color if "color" in fields else None,
            icon=self.icon if "icon" in fields else None,
        )


class CategoryOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_ids: list[Annotated[int, Field(gt=0)]]


class CategoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    color: CategoryColor
    icon: CategoryIcon
    sort_order: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_result(cls, result: CategoryView) -> CategoryResponse:
        return cls(**result.__dict__)


class CategoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[CategoryResponse]
    supported_colors: list[CategoryColor]
    supported_icons: list[CategoryIcon]

    @classmethod
    def from_result(cls, result: CategoryListResult) -> CategoryListResponse:
        return cls(
            categories=[CategoryResponse.from_result(item) for item in result.categories],
            supported_colors=list(result.supported_colors),
            supported_icons=list(result.supported_icons),
        )

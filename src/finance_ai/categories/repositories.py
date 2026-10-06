from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from finance_ai.categories.schemas import CategoryColor, CategoryIcon


class CategoryNameConstraintError(Exception):
    """Raised when the authoritative per-owner category-name constraint rejects a write."""


@dataclass(frozen=True)
class CategoryRecord:
    id: int
    user_id: int
    name: str
    color: CategoryColor
    icon: CategoryIcon
    sort_order: int
    created_at: datetime
    updated_at: datetime


class CategoryRepository(Protocol):
    def lock_owner(self, *, owner_id: int) -> bool: ...

    def list_owned(self, *, owner_id: int) -> list[CategoryRecord]: ...

    def find_owned(
        self, *, owner_id: int, category_id: int, for_update: bool = False
    ) -> CategoryRecord | None: ...

    def name_exists(
        self, *, owner_id: int, normalized_name: str, excluding_id: int | None = None
    ) -> bool: ...

    def add(
        self,
        *,
        owner_id: int,
        name: str,
        color: CategoryColor,
        icon: CategoryIcon,
        sort_order: int,
    ) -> int: ...

    def update(
        self,
        *,
        owner_id: int,
        category_id: int,
        name: str | None,
        color: CategoryColor | None,
        icon: CategoryIcon | None,
    ) -> None: ...

    def reorder(self, *, owner_id: int, category_ids: tuple[int, ...]) -> None: ...

    def delete(self, *, owner_id: int, category_id: int) -> None: ...

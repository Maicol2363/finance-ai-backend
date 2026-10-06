from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from finance_ai.categories.policy import CategoryPolicy
from finance_ai.categories.repositories import CategoryNameConstraintError, CategoryRecord
from finance_ai.categories.schemas import (
    CategoryColor,
    CategoryCreateCommand,
    CategoryIcon,
    CategoryUpdateCommand,
)
from finance_ai.categories.service import CategoryService
from finance_ai.core.errors import (
    CategoryNameConflictError,
    CategoryNotFoundError,
    CategoryOrderInvalidError,
    RequestInvalidError,
)

NOW = datetime(2026, 9, 12, tzinfo=UTC)


class FakeRepository:
    def __init__(self) -> None:
        self.items: dict[int, CategoryRecord] = {}
        self.next_id = 1
        self.constraint_failure = False
        self.locked: list[int] = []

    def lock_owner(self, *, owner_id: int) -> bool:
        self.locked.append(owner_id)
        return True

    def list_owned(self, *, owner_id: int) -> list[CategoryRecord]:
        return sorted(
            (item for item in self.items.values() if item.user_id == owner_id),
            key=lambda item: (item.sort_order, item.id),
        )

    def find_owned(
        self, *, owner_id: int, category_id: int, for_update: bool = False
    ) -> CategoryRecord | None:
        item = self.items.get(category_id)
        return item if item is not None and item.user_id == owner_id else None

    def name_exists(
        self, *, owner_id: int, normalized_name: str, excluding_id: int | None = None
    ) -> bool:
        return any(
            item.user_id == owner_id
            and item.id != excluding_id
            and item.name.lower() == normalized_name.lower()
            for item in self.items.values()
        )

    def add(
        self,
        *,
        owner_id: int,
        name: str,
        color: CategoryColor,
        icon: CategoryIcon,
        sort_order: int,
    ) -> int:
        if self.constraint_failure:
            raise CategoryNameConstraintError
        item_id = self.next_id
        self.next_id += 1
        self.items[item_id] = CategoryRecord(
            id=item_id,
            user_id=owner_id,
            name=name,
            color=color,
            icon=icon,
            sort_order=sort_order,
            created_at=NOW,
            updated_at=NOW,
        )
        return item_id

    def update(
        self,
        *,
        owner_id: int,
        category_id: int,
        name: str | None,
        color: CategoryColor | None,
        icon: CategoryIcon | None,
    ) -> None:
        if self.constraint_failure:
            raise CategoryNameConstraintError
        item = self.items[category_id]
        self.items[category_id] = replace(
            item,
            name=name if name is not None else item.name,
            color=color if color is not None else item.color,
            icon=icon if icon is not None else item.icon,
        )

    def reorder(self, *, owner_id: int, category_ids: tuple[int, ...]) -> None:
        for index, item_id in enumerate(category_ids):
            self.items[item_id] = replace(self.items[item_id], sort_order=index)

    def delete(self, *, owner_id: int, category_id: int) -> None:
        del self.items[category_id]


class FakeUow:
    def __init__(self, repository: FakeRepository) -> None:
        self.categories = repository
        self.commits = 0

    def __enter__(self) -> FakeUow:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


@pytest.fixture
def category_context() -> tuple[CategoryService, FakeRepository, FakeUow]:
    repository = FakeRepository()
    uow = FakeUow(repository)
    return CategoryService(policy=CategoryPolicy(), uow_factory=lambda: uow), repository, uow


def create_category(service: CategoryService, owner_id: int, name: str) -> int:
    return service.create(
        owner_id=owner_id,
        command=CategoryCreateCommand(name=name, color=CategoryColor.BLUE, icon=CategoryIcon.OTHER),
    ).id


def test_complete_owned_category_lifecycle_and_gap_free_order(
    category_context: tuple[CategoryService, FakeRepository, FakeUow],
) -> None:
    service, repository, uow = category_context
    first_id = create_category(service, 7, " Needs ")
    second_id = create_category(service, 7, "Wants")
    foreign_id = create_category(service, 8, "Needs")

    assert [item.name for item in service.list(owner_id=7).categories] == ["Needs", "Wants"]
    assert len(service.list(owner_id=7).supported_colors) == 9
    assert len(service.list(owner_id=7).supported_icons) == 13
    assert service.get(owner_id=7, category_id=first_id).name == "Needs"

    updated = service.update(
        owner_id=7,
        category_id=second_id,
        command=CategoryUpdateCommand(
            name="Fun", color=CategoryColor.PINK, icon=CategoryIcon.ENTERTAINMENT
        ),
    )
    assert (updated.name, updated.color, updated.icon) == (
        "Fun",
        CategoryColor.PINK,
        CategoryIcon.ENTERTAINMENT,
    )
    reordered = service.reorder(owner_id=7, category_ids=[second_id, first_id])
    assert [item.id for item in reordered.categories] == [second_id, first_id]

    service.delete(owner_id=7, category_id=second_id)
    assert [(item.id, item.sort_order) for item in service.list(owner_id=7).categories] == [
        (first_id, 0)
    ]
    assert repository.items[foreign_id].name == "Needs"
    assert repository.locked == [7, 7, 8, 7, 7, 7]
    assert uow.commits == 6


def test_name_conflicts_order_validation_and_ownership_are_safe(
    category_context: tuple[CategoryService, FakeRepository, FakeUow],
) -> None:
    service, repository, _ = category_context
    first_id = create_category(service, 7, "Needs")
    second_id = create_category(service, 7, "Wants")

    with pytest.raises(CategoryNameConflictError):
        create_category(service, 7, " needs ")
    with pytest.raises(CategoryNameConflictError):
        service.update(
            owner_id=7,
            category_id=second_id,
            command=CategoryUpdateCommand(name="NEEDS"),
        )
    for invalid in ([first_id], [first_id, first_id], [first_id, 999]):
        with pytest.raises(CategoryOrderInvalidError):
            service.reorder(owner_id=7, category_ids=invalid)
    with pytest.raises(CategoryNotFoundError):
        service.get(owner_id=8, category_id=first_id)
    with pytest.raises(CategoryNotFoundError):
        service.delete(owner_id=8, category_id=first_id)

    repository.constraint_failure = True
    with pytest.raises(CategoryNameConflictError):
        create_category(service, 7, "Other")
    with pytest.raises(CategoryNameConflictError):
        service.update(
            owner_id=7,
            category_id=first_id,
            command=CategoryUpdateCommand(name="Essentials"),
        )


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_policy_rejects_invalid_names(name: str) -> None:
    with pytest.raises(RequestInvalidError):
        CategoryPolicy.normalize_name(name)


def test_policy_rejects_empty_domain_update() -> None:
    with pytest.raises(RequestInvalidError):
        CategoryPolicy.validate_update(CategoryUpdateCommand())

from collections.abc import Callable, Sequence

from finance_ai.categories.policy import CategoryPolicy
from finance_ai.categories.repositories import CategoryNameConstraintError, CategoryRecord
from finance_ai.categories.schemas import (
    CategoryColor,
    CategoryCreateCommand,
    CategoryIcon,
    CategoryListResult,
    CategoryUpdateCommand,
    CategoryView,
)
from finance_ai.categories.uow import CategoryUnitOfWork
from finance_ai.core.errors import (
    CategoryNameConflictError,
    CategoryNotFoundError,
    CategoryOrderInvalidError,
)

CategoryUnitOfWorkFactory = Callable[[], CategoryUnitOfWork]


class CategoryService:
    def __init__(self, *, policy: CategoryPolicy, uow_factory: CategoryUnitOfWorkFactory) -> None:
        self.policy = policy
        self.uow_factory = uow_factory

    def create(self, *, owner_id: int, command: CategoryCreateCommand) -> CategoryView:
        name = self.policy.normalize_name(command.name)
        color = self.policy.validate_color(command.color)
        icon = self.policy.validate_icon(command.icon)
        with self.uow_factory() as uow:
            uow.categories.lock_owner(owner_id=owner_id)
            existing = uow.categories.list_owned(owner_id=owner_id)
            if uow.categories.name_exists(owner_id=owner_id, normalized_name=name):
                raise CategoryNameConflictError
            try:
                category_id = uow.categories.add(
                    owner_id=owner_id,
                    name=name,
                    color=color,
                    icon=icon,
                    sort_order=len(existing),
                )
                uow.flush()
            except CategoryNameConstraintError:
                raise CategoryNameConflictError from None
            created = self._require_owned(uow, owner_id=owner_id, category_id=category_id)
            uow.commit()
            return self._view(created)

    def list(self, *, owner_id: int) -> CategoryListResult:
        with self.uow_factory() as uow:
            categories = uow.categories.list_owned(owner_id=owner_id)
        return CategoryListResult(
            categories=tuple(self._view(item) for item in categories),
            supported_colors=tuple(CategoryColor),
            supported_icons=tuple(CategoryIcon),
        )

    def get(self, *, owner_id: int, category_id: int) -> CategoryView:
        with self.uow_factory() as uow:
            return self._view(self._require_owned(uow, owner_id=owner_id, category_id=category_id))

    def update(
        self, *, owner_id: int, category_id: int, command: CategoryUpdateCommand
    ) -> CategoryView:
        self.policy.validate_update(command)
        name = self.policy.normalize_name(command.name) if command.name is not None else None
        color = self.policy.validate_color(command.color) if command.color is not None else None
        icon = self.policy.validate_icon(command.icon) if command.icon is not None else None
        with self.uow_factory() as uow:
            uow.categories.lock_owner(owner_id=owner_id)
            self._require_owned(uow, owner_id=owner_id, category_id=category_id, for_update=True)
            if name is not None and uow.categories.name_exists(
                owner_id=owner_id, normalized_name=name, excluding_id=category_id
            ):
                raise CategoryNameConflictError
            try:
                uow.categories.update(
                    owner_id=owner_id,
                    category_id=category_id,
                    name=name,
                    color=color,
                    icon=icon,
                )
                uow.flush()
            except CategoryNameConstraintError:
                raise CategoryNameConflictError from None
            updated = self._require_owned(uow, owner_id=owner_id, category_id=category_id)
            uow.commit()
            return self._view(updated)

    def reorder(self, *, owner_id: int, category_ids: Sequence[int]) -> CategoryListResult:
        with self.uow_factory() as uow:
            uow.categories.lock_owner(owner_id=owner_id)
            current = uow.categories.list_owned(owner_id=owner_id)
            expected = {item.id for item in current}
            if len(category_ids) != len(expected) or set(category_ids) != expected:
                raise CategoryOrderInvalidError
            uow.categories.reorder(owner_id=owner_id, category_ids=tuple(category_ids))
            uow.flush()
            reordered = uow.categories.list_owned(owner_id=owner_id)
            uow.commit()
        return CategoryListResult(
            categories=tuple(self._view(item) for item in reordered),
            supported_colors=tuple(CategoryColor),
            supported_icons=tuple(CategoryIcon),
        )

    def delete(self, *, owner_id: int, category_id: int) -> None:
        with self.uow_factory() as uow:
            uow.categories.lock_owner(owner_id=owner_id)
            self._require_owned(uow, owner_id=owner_id, category_id=category_id, for_update=True)
            uow.categories.delete(owner_id=owner_id, category_id=category_id)
            remaining = uow.categories.list_owned(owner_id=owner_id)
            uow.categories.reorder(
                owner_id=owner_id, category_ids=tuple(item.id for item in remaining)
            )
            uow.flush()
            uow.commit()

    @staticmethod
    def _require_owned(
        uow: CategoryUnitOfWork,
        *,
        owner_id: int,
        category_id: int,
        for_update: bool = False,
    ) -> CategoryRecord:
        category = uow.categories.find_owned(
            owner_id=owner_id, category_id=category_id, for_update=for_update
        )
        if category is None:
            raise CategoryNotFoundError
        return category

    @staticmethod
    def _view(record: CategoryRecord) -> CategoryView:
        return CategoryView(
            id=record.id,
            name=record.name,
            color=record.color,
            icon=record.icon,
            sort_order=record.sort_order,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

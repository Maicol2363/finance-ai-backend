from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from finance_ai.categories.repositories import CategoryNameConstraintError, CategoryRecord
from finance_ai.categories.schemas import CategoryColor, CategoryIcon
from finance_ai.db.models.catalogs import Category
from finance_ai.db.models.identity import AppUser

CATEGORY_NAME_CONSTRAINT = "uq_category_user_name"


def is_category_name_conflict(exc: BaseException) -> bool:
    if not isinstance(exc, IntegrityError):
        return False
    diagnostic = getattr(exc.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == CATEGORY_NAME_CONSTRAINT


class SQLAlchemyCategoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_owner(self, *, owner_id: int) -> bool:
        return (
            self.session.scalar(
                select(AppUser.id).where(AppUser.id == owner_id).with_for_update().limit(1)
            )
            is not None
        )

    def list_owned(self, *, owner_id: int) -> list[CategoryRecord]:
        values = self.session.scalars(
            select(Category)
            .where(Category.user_id == owner_id)
            .order_by(Category.sort_order, Category.id)
        )
        return [self._record(item) for item in values]

    def find_owned(
        self, *, owner_id: int, category_id: int, for_update: bool = False
    ) -> CategoryRecord | None:
        statement = select(Category).where(Category.user_id == owner_id, Category.id == category_id)
        if for_update:
            statement = statement.with_for_update()
        item = self.session.scalar(statement)
        return None if item is None else self._record(item)

    def name_exists(
        self, *, owner_id: int, normalized_name: str, excluding_id: int | None = None
    ) -> bool:
        statement = select(Category.id).where(
            Category.user_id == owner_id,
            func.lower(Category.name) == normalized_name.lower(),
        )
        if excluding_id is not None:
            statement = statement.where(Category.id != excluding_id)
        return self.session.scalar(statement.limit(1)) is not None

    def add(
        self,
        *,
        owner_id: int,
        name: str,
        color: CategoryColor,
        icon: CategoryIcon,
        sort_order: int,
    ) -> int:
        item = Category(
            user_id=owner_id,
            name=name,
            color=color.value,
            icon=icon.value,
            sort_order=sort_order,
        )
        self.session.add(item)
        try:
            self.session.flush()
        except IntegrityError as exc:
            if is_category_name_conflict(exc):
                raise CategoryNameConstraintError from None
            raise
        return item.id

    def update(
        self,
        *,
        owner_id: int,
        category_id: int,
        name: str | None,
        color: CategoryColor | None,
        icon: CategoryIcon | None,
    ) -> None:
        values: dict[str, object] = {"updated_at": func.now()}
        if name is not None:
            values["name"] = name
        if color is not None:
            values["color"] = color.value
        if icon is not None:
            values["icon"] = icon.value
        try:
            self.session.execute(
                update(Category)
                .where(Category.user_id == owner_id, Category.id == category_id)
                .values(**values)
            )
        except IntegrityError as exc:
            if is_category_name_conflict(exc):
                raise CategoryNameConstraintError from None
            raise

    def reorder(self, *, owner_id: int, category_ids: tuple[int, ...]) -> None:
        for position, category_id in enumerate(category_ids):
            self.session.execute(
                update(Category)
                .where(Category.user_id == owner_id, Category.id == category_id)
                .values(sort_order=position, updated_at=func.now())
            )

    def delete(self, *, owner_id: int, category_id: int) -> None:
        self.session.execute(
            delete(Category).where(Category.user_id == owner_id, Category.id == category_id)
        )

    @staticmethod
    def _record(item: Category) -> CategoryRecord:
        return CategoryRecord(
            id=item.id,
            user_id=item.user_id,
            name=item.name,
            color=CategoryColor(item.color),
            icon=CategoryIcon(item.icon),
            sort_order=item.sort_order,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

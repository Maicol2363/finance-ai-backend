from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from finance_ai.categories.repositories import CategoryNameConstraintError, CategoryRepository
from finance_ai.db.repositories.categories import (
    SQLAlchemyCategoryRepository,
    is_category_name_conflict,
)


class SQLAlchemyCategoryUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.session: Session | None = None
        self.categories: CategoryRepository
        self._committed = False

    def __enter__(self) -> Self:
        self.session = self.session_factory()
        self.categories = SQLAlchemyCategoryRepository(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.session is None:
            return
        try:
            if exc_type is not None or not self._committed:
                self.session.rollback()
        finally:
            self.session.close()

    def flush(self) -> None:
        try:
            self._require_session().flush()
        except Exception as exc:
            if is_category_name_conflict(exc):
                raise CategoryNameConstraintError from None
            raise

    def commit(self) -> None:
        self._require_session().commit()
        self._committed = True

    def rollback(self) -> None:
        self._require_session().rollback()

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("Unit of Work has not been entered")
        return self.session

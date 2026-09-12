from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from finance_ai.accounts.repositories import (
    AccountRepository,
    ActiveAccountNameConstraintError,
    CurrencyRepository,
)
from finance_ai.db.repositories.accounts import (
    SQLAlchemyAccountCurrencyRepository,
    SQLAlchemyAccountRepository,
    is_active_name_conflict,
)


class SQLAlchemyAccountUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.session: Session | None = None
        self.accounts: AccountRepository
        self.currencies: CurrencyRepository
        self._committed = False

    def __enter__(self) -> Self:
        self.session = self.session_factory()
        self.accounts = SQLAlchemyAccountRepository(self.session)
        self.currencies = SQLAlchemyAccountCurrencyRepository(self.session)
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
            if is_active_name_conflict(exc):
                raise ActiveAccountNameConstraintError from None
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

from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.session_repositories import (
    SecurityEventRepository,
    SessionRepository,
    SessionUserRepository,
)
from finance_ai.db.repositories.sessions import (
    SQLAlchemySessionRepository,
    SQLAlchemySessionUserRepository,
)


class SQLAlchemySecurityEventRepository:
    """Stages customer sign-in evidence through the controlled SEC-02 writer."""

    def __init__(self, session: Session, writer: SecurityAuditWriter | None = None) -> None:
        self.session = session
        self.writer = writer or SecurityAuditWriter()

    def record(
        self,
        *,
        event_type: str,
        actor_id: int | None,
        result: str,
        correlation_id: str,
        context: dict[str, str],
    ) -> None:
        actor = None if actor_id is None else str(actor_id)
        self.writer.append(
            self.session,
            event_type=event_type,
            actor_id=actor,
            # The affected account is the signing-in account itself, when it is known.
            target_id=actor,
            target_type=None if actor is None else "account",
            result=result,
            correlation_id=correlation_id,
            context=context,
        )


class SQLAlchemySessionUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self.session: Session | None = None
        self.users: SessionUserRepository
        self.sessions: SessionRepository
        self.security_events: SecurityEventRepository
        self._committed = False

    def __enter__(self) -> Self:
        self.session = self.session_factory()
        self.users = SQLAlchemySessionUserRepository(self.session)
        self.sessions = SQLAlchemySessionRepository(self.session)
        self.security_events = SQLAlchemySecurityEventRepository(self.session)
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
        self._require_session().flush()

    def commit(self) -> None:
        self._require_session().commit()
        self._committed = True

    def rollback(self) -> None:
        self._require_session().rollback()

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("Unit of Work has not been entered")
        return self.session

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.core.errors import (
    StaffAccessDeniedError,
    SupportTicketClaimConflictError,
    SupportTicketNotFoundError,
)
from finance_ai.db.models.support import SupportTicket, SupportTicketActivity
from finance_ai.staff_auth.schemas import StaffSessionUser
from finance_ai.support.schemas import (
    TicketClaimResult,
    TicketDetail,
    TicketQueueItem,
    TicketStatus,
)


class SupportTicketService:
    def __init__(
        self, session_factory: sessionmaker[Session], audit_writer: SecurityAuditWriter
    ) -> None:
        self._session_factory = session_factory
        self._audit_writer = audit_writer

    @staticmethod
    def _require_agent(user: StaffSessionUser) -> None:
        if user.role != "support_agent":
            raise StaffAccessDeniedError()

    def list_queue(
        self, user: StaffSessionUser, status: TicketStatus | None
    ) -> list[TicketQueueItem]:
        self._require_agent(user)
        statement = select(
            SupportTicket.id,
            SupportTicket.subject,
            SupportTicket.status,
            SupportTicket.created_at,
            SupportTicket.assigned_staff_user_id,
        )
        if status is not None:
            statement = statement.where(SupportTicket.status == status.value)
        statement = statement.order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
        with self._session_factory() as session:
            rows = session.execute(statement).mappings().all()
        return [TicketQueueItem.model_validate(row) for row in rows]

    def get_assigned(self, user: StaffSessionUser, ticket_id: int) -> TicketDetail:
        self._require_agent(user)
        statement = select(
            SupportTicket.id,
            SupportTicket.subject,
            SupportTicket.status,
            SupportTicket.created_at,
            SupportTicket.assigned_staff_user_id,
            SupportTicket.category,
            SupportTicket.description,
        ).where(
            SupportTicket.id == ticket_id,
            (SupportTicket.assigned_staff_user_id == user.id)
            | (
                (SupportTicket.originating_support_agent_id == user.id)
                & (SupportTicket.status == TicketStatus.ESCALATED.value)
            ),
        )
        with self._session_factory() as session:
            row = session.execute(statement).mappings().one_or_none()
        if row is None:
            raise SupportTicketNotFoundError()
        return TicketDetail.model_validate(row)

    def claim(
        self, user: StaffSessionUser, ticket_id: int, correlation_id: str
    ) -> TicketClaimResult:
        self._require_agent(user)
        with self._session_factory() as session:
            statement = (
                update(SupportTicket)
                .where(
                    SupportTicket.id == ticket_id,
                    SupportTicket.assigned_staff_user_id.is_(None),
                    SupportTicket.status == TicketStatus.OPEN.value,
                )
                .values(assigned_staff_user_id=user.id, status=TicketStatus.IN_PROGRESS.value)
                .returning(
                    SupportTicket.id,
                    SupportTicket.subject,
                    SupportTicket.status,
                    SupportTicket.created_at,
                    SupportTicket.assigned_staff_user_id,
                )
            )
            row = session.execute(statement).mappings().one_or_none()
            if row is None:
                exists = session.scalar(
                    select(SupportTicket.id).where(SupportTicket.id == ticket_id)
                )
                if exists is None:
                    raise SupportTicketNotFoundError()
                raise SupportTicketClaimConflictError()

            session.add(
                SupportTicketActivity(
                    ticket_id=ticket_id,
                    actor_user_id=user.id,
                    event_type="ticket_claimed",
                    previous_status=TicketStatus.OPEN.value,
                    new_status=TicketStatus.IN_PROGRESS.value,
                    previous_assignee_user_id=None,
                    new_assignee_user_id=user.id,
                )
            )
            self._audit_writer.append(
                session,
                event_type="support.ticket_claimed",
                actor_id=str(user.id),
                result="success",
                correlation_id=correlation_id,
                context={},
                target_id=str(ticket_id),
            )
            session.commit()
        return TicketClaimResult.model_validate(row)

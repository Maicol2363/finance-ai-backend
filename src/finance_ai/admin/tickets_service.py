"""ADM-05 escalated support tickets.

Boundaries enforced here (not by the frontend):

* only ``escalated`` tickets are reachable; every mutation needs current ownership;
* an administrator claims a case with one conditional UPDATE, so two administrators can never
  both own it;
* only the case-context fields below are exposed: no balances, transactions, receipts or
  credentials, whatever the ticket says;
* escalation and return reasons are staff-only workflow metadata; customer projections never
  include them (they come from ``support.schemas.CustomerTicket`` / ``CustomerConversation``);
* the return to Support runs procedure P2, which changes status, assignment and ownership
  atomically; every state change appends activity history and a SEC-02 event in the same
  transaction, without copying conversation text into the audit store.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.admin.schemas import (
    AdminCase,
    EscalatedTicketItem,
    EscalationOrigin,
    EscalationScope,
    ReturnTicketResult,
)
from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.auth.session_policy import Clock
from finance_ai.core.errors import (
    AdminRequestInvalidError,
    AuditUnavailableError,
    EscalationClaimConflictError,
    StaffAccessDeniedError,
    SupportTicketNotFoundError,
    SupportTicketStateConflictError,
)
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.support import SupportTicket, SupportTicketActivity, SupportTicketMessage
from finance_ai.support.repository import SupportRepository

ESCALATED: Final = "escalated"
MAX_QUEUE_ROWS: Final = 100

_RETURN_TICKET = text(
    "CALL sp_admin_return_escalated_ticket("
    "CAST(:admin AS bigint), CAST(:ticket AS bigint), CAST(:reason AS text), "
    "CAST(NULL AS text), CAST(NULL AS text), CAST(NULL AS bigint))"
)


class AdminTicketService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        audit_writer: SecurityAuditWriter,
        clock: Clock,
    ) -> None:
        self._factory = session_factory
        self._audit = audit_writer
        self._clock = clock

    # --- reads ----------------------------------------------------------------------------

    def list_escalated(
        self, *, admin_id: int, scope: EscalationScope, limit: int
    ) -> list[EscalatedTicketItem]:
        bounded = max(1, min(limit, MAX_QUEUE_ROWS))
        statement = select(
            SupportTicket.id,
            SupportTicket.subject,
            SupportTicket.category,
            SupportTicket.created_at,
            SupportTicket.admin_owner_user_id,
            SupportTicket.originating_support_agent_id,
        ).where(SupportTicket.status == ESCALATED)
        if scope == "unclaimed":
            statement = statement.where(SupportTicket.admin_owner_user_id.is_(None))
        elif scope == "mine":
            statement = statement.where(SupportTicket.admin_owner_user_id == admin_id)
        statement = statement.order_by(SupportTicket.created_at, SupportTicket.id).limit(bounded)
        with self._factory() as session:
            rows = session.execute(statement).all()
        return [
            EscalatedTicketItem(
                id=row.id,
                subject=row.subject,
                category=row.category,
                created_at=row.created_at,
                owner_admin_id=row.admin_owner_user_id,
                originating_agent_id=row.originating_support_agent_id,
                owned_by_me=row.admin_owner_user_id == admin_id,
            )
            for row in rows
        ]

    def get_case(self, *, admin_id: int, ticket_id: int) -> AdminCase:
        with self._factory() as session:
            return self._case(session, admin_id, ticket_id)

    # --- mutations ------------------------------------------------------------------------

    def claim(self, *, admin_id: int, ticket_id: int, correlation_id: str) -> AdminCase:
        """One conditional UPDATE decides the winner; the loser gets a clear conflict."""
        try:
            with self._factory() as session, session.begin():
                claimed = session.execute(
                    update(SupportTicket)
                    .where(
                        SupportTicket.id == ticket_id,
                        SupportTicket.status == ESCALATED,
                        SupportTicket.admin_owner_user_id.is_(None),
                    )
                    .values(admin_owner_user_id=admin_id)
                    .returning(SupportTicket.id, SupportTicket.assigned_staff_user_id)
                ).one_or_none()
                if claimed is None:
                    exists = session.scalar(
                        select(SupportTicket.id).where(
                            SupportTicket.id == ticket_id, SupportTicket.status == ESCALATED
                        )
                    )
                    if exists is None:
                        raise SupportTicketNotFoundError()
                    raise EscalationClaimConflictError()
                self._activity(
                    session,
                    ticket_id,
                    admin_id,
                    "ticket_admin_claimed",
                    previous_assignee=claimed.assigned_staff_user_id,
                    new_assignee=admin_id,
                )
                self._audit_event(
                    session,
                    "support.ticket_claimed",
                    admin_id,
                    ticket_id,
                    correlation_id,
                    "escalation_claimed",
                )
                return self._case(session, admin_id, ticket_id)
        except SupportTicketNotFoundError, EscalationClaimConflictError:
            raise
        except Exception as exc:
            raise AuditUnavailableError() from exc

    def reply(self, *, admin_id: int, ticket_id: int, body: str, correlation_id: str) -> None:
        """Customer-visible message through the existing conversation (no private notes)."""
        try:
            with self._factory() as session, session.begin():
                self._lock_owned(session, admin_id, ticket_id)
                session.add(
                    SupportTicketMessage(
                        ticket_id=ticket_id,
                        author_user_id=admin_id,
                        body=body,
                        created_at=self._clock.now(),
                    )
                )
                self._activity(session, ticket_id, admin_id, "message_sent")
                self._audit_event(
                    session, "support.message_sent", admin_id, ticket_id, correlation_id, "reply"
                )
        except SupportTicketNotFoundError, SupportTicketStateConflictError:
            raise
        except Exception as exc:
            raise AuditUnavailableError() from exc

    def resolve(self, *, admin_id: int, ticket_id: int, summary: str, correlation_id: str) -> None:
        """Resolve with a customer-visible summary; history stays, ownership is released."""
        try:
            with self._factory() as session, session.begin():
                self._lock_owned(session, admin_id, ticket_id)
                session.execute(
                    update(SupportTicket)
                    .where(SupportTicket.id == ticket_id)
                    .values(
                        status="resolved",
                        resolution_summary=summary,
                        admin_owner_user_id=None,
                        updated_at=self._clock.now(),
                    )
                )
                self._activity(
                    session,
                    ticket_id,
                    admin_id,
                    "ticket_resolved",
                    previous_status=ESCALATED,
                    new_status="resolved",
                )
                self._audit_event(
                    session,
                    "support.ticket_resolved",
                    admin_id,
                    ticket_id,
                    correlation_id,
                    "escalation_resolved",
                )
        except SupportTicketNotFoundError, SupportTicketStateConflictError:
            raise
        except Exception as exc:
            raise AuditUnavailableError() from exc

    def return_to_support(
        self, *, admin_id: int, ticket_id: int, reason: str, correlation_id: str
    ) -> ReturnTicketResult:
        """Procedure P2: atomic status + assignment + history, plus the SEC-02 event."""
        try:
            with self._factory() as session, session.begin():
                row = session.execute(
                    _RETURN_TICKET, {"admin": admin_id, "ticket": ticket_id, "reason": reason}
                ).one()
                result, new_status, new_assignee = row[0], row[1], row[2]
                if result == "ok":
                    self._audit_event(
                        session,
                        "support.ticket_returned",
                        admin_id,
                        ticket_id,
                        correlation_id,
                        "returned_to_agent" if new_status == "in_progress" else "returned_to_queue",
                    )
        except Exception as exc:
            raise AuditUnavailableError() from exc
        if result == "ok":
            return ReturnTicketResult(status=new_status, assigned_staff_user_id=new_assignee)
        if result == "not_found":
            raise SupportTicketNotFoundError()
        if result in {"not_escalated", "not_owner"}:
            raise SupportTicketStateConflictError()
        if result == "invalid_reason":
            raise AdminRequestInvalidError({"reason": ["Enter a return reason."]})
        raise StaffAccessDeniedError()

    # --- helpers --------------------------------------------------------------------------

    def _case(self, session: Session, admin_id: int, ticket_id: int) -> AdminCase:
        table = SupportTicket.__table__
        columns = (
            "id",
            "subject",
            "category",
            "description",
            "status",
            "created_at",
            "assigned_staff_user_id",
            "resolution_summary",
        )
        row = (
            session.execute(
                select(*(table.c[name] for name in columns)).where(
                    table.c.id == ticket_id, table.c.status == ESCALATED
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise SupportTicketNotFoundError()
        links = session.execute(
            select(
                SupportTicket.customer_user_id,
                SupportTicket.admin_owner_user_id,
                SupportTicket.originating_support_agent_id,
            ).where(SupportTicket.id == ticket_id)
        ).one()
        repository = SupportRepository(session)
        history = repository.history(ticket_id)
        escalation_reason = next(
            (
                entry.internal_reason
                for entry in reversed(history)
                if entry.event_type == "ticket_escalated"
            ),
            None,
        )
        origin_id = links.originating_support_agent_id
        return AdminCase.model_validate(
            {
                "ticket": dict(row),
                "customer": repository.ticket_customer(links.customer_user_id),
                "messages": repository.messages(ticket_id),
                "history": history,
                "escalation_reason": escalation_reason,
                "owner_admin_id": links.admin_owner_user_id,
                "owned_by_me": links.admin_owner_user_id == admin_id,
                "originating_agent": EscalationOrigin(
                    user_id=origin_id,
                    can_receive_return=self._agent_can_receive_return(session, origin_id),
                ),
            }
        )

    @staticmethod
    def _agent_can_receive_return(session: Session, agent_id: int | None) -> bool:
        if agent_id is None:
            return False
        return (
            session.scalar(
                select(AppUser.id)
                .join(Role, Role.id == AppUser.role_id)
                .where(
                    AppUser.id == agent_id,
                    AppUser.is_active.is_(True),
                    Role.name == "support_agent",
                )
            )
            is not None
        )

    @staticmethod
    def _lock_owned(session: Session, admin_id: int, ticket_id: int) -> None:
        row = session.execute(
            select(SupportTicket.status, SupportTicket.admin_owner_user_id)
            .where(SupportTicket.id == ticket_id)
            .with_for_update()
        ).one_or_none()
        if row is None or row.status != ESCALATED:
            raise SupportTicketNotFoundError()
        if row.admin_owner_user_id != admin_id:
            raise SupportTicketStateConflictError()

    def _activity(
        self,
        session: Session,
        ticket_id: int,
        actor_id: int,
        event_type: str,
        *,
        previous_status: str | None = None,
        new_status: str | None = None,
        previous_assignee: int | None = None,
        new_assignee: int | None = None,
    ) -> None:
        session.add(
            SupportTicketActivity(
                ticket_id=ticket_id,
                actor_user_id=actor_id,
                event_type=event_type,
                previous_status=previous_status,
                new_status=new_status,
                previous_assignee_user_id=previous_assignee,
                new_assignee_user_id=new_assignee,
                created_at=self._clock.now(),
            )
        )

    def _audit_event(
        self,
        session: Session,
        event_type: str,
        admin_id: int,
        ticket_id: int,
        correlation_id: str,
        reason: str,
    ) -> None:
        self._audit.append(
            session,
            event_type=event_type,
            actor_id=str(admin_id),
            target_id=str(ticket_id),
            target_type="support_ticket",
            result="success",
            correlation_id=correlation_id,
            context={"reason": reason},
        )

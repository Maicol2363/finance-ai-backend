"""Support application unit of work: authorization, mutation and audit commit together."""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.core.errors import SupportTicketStateConflictError
from finance_ai.db.models.support import SupportTicket, SupportTicketActivity, SupportTicketMessage
from finance_ai.support.repository import SupportRepository
from finance_ai.support.schemas import (
    AgentTicketCreate,
    CustomerConversation,
    CustomerMetadata,
    CustomerTicket,
    StaffConversation,
    TicketCreate,
)


class TicketWorkflow:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.factory = session_factory
        self.audit = SecurityAuditWriter()

    def _event(
        self,
        session: Session,
        ticket_id: int,
        actor: int,
        event: str,
        correlation: str,
        *,
        previous: str | None = None,
        status: str | None = None,
        reason: str | None = None,
    ) -> None:
        session.add(
            SupportTicketActivity(
                ticket_id=ticket_id,
                actor_user_id=actor,
                event_type=event,
                previous_status=previous,
                new_status=status,
                internal_reason=reason,
                created_at=datetime.now(UTC),
            )
        )
        self.audit.append(
            session,
            event_type="ticket.escalated" if event == "ticket_escalated" else f"support.{event}",
            actor_id=str(actor),
            target_id=str(ticket_id),
            result="success",
            correlation_id=correlation,
            context={},
        )

    def lookup(self, actor: int, identifier: str, correlation: str) -> CustomerMetadata:
        with self.factory.begin() as session:
            repo = SupportRepository(session)
            repo.require_role(actor, "support_agent")
            customer = repo.customer(identifier)
            self.audit.append(
                session,
                event_type="support.customer_lookup",
                actor_id=str(actor),
                result="success",
                correlation_id=correlation,
                context={},
            )
            return customer

    def create(
        self, actor: int, data: TicketCreate, correlation: str, *, staff: bool
    ) -> CustomerTicket:
        with self.factory.begin() as session:
            repo = SupportRepository(session)
            repo.require_role(actor, "support_agent" if staff else "user")
            customer_id = actor
            if staff:
                assert isinstance(data, AgentTicketCreate)
                customer_id = repo.customer(data.customer_identifier).id
            ticket = SupportTicket(
                customer_user_id=customer_id,
                created_by_user_id=actor,
                subject=data.subject,
                category=data.category,
                description=data.description,
                status="open",
            )
            session.add(ticket)
            session.flush()
            self._event(session, ticket.id, actor, "ticket_created", correlation, status="open")
            # Return a creation receipt without granting conversation access.
            return CustomerTicket(
                id=ticket.id,
                subject=ticket.subject,
                category=ticket.category,
                description=ticket.description,
                status=ticket.status,
                created_at=ticket.created_at,
                assigned_staff_user_id=None,
                resolution_summary=None,
            )

    def list_own(self, actor: int) -> list[CustomerTicket]:
        with self.factory.begin() as session:
            repo = SupportRepository(session)
            repo.require_role(actor, "user")
            ids = session.scalars(
                select(SupportTicket.id)
                .where(SupportTicket.customer_user_id == actor)
                .order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
            )
            return [repo.ticket(ticket_id, actor, staff=False) for ticket_id in ids]

    def read(
        self, actor: int, ticket_id: int, *, staff: bool
    ) -> CustomerConversation | StaffConversation:
        with self.factory.begin() as session:
            repo = SupportRepository(session)
            repo.require_role(actor, "support_agent" if staff else "user")
            ticket = repo.ticket(ticket_id, actor, staff=staff)
            messages = repo.messages(ticket_id)
            if staff:
                return StaffConversation(
                    ticket=ticket,
                    messages=messages,
                    customer=repo.customer(str(repo.customer_id(ticket_id))),
                    history=repo.history(ticket_id),
                )
            return CustomerConversation(ticket=ticket, messages=messages)

    def message(
        self, actor: int, ticket_id: int, body: str, correlation: str, *, staff: bool
    ) -> None:
        with self.factory.begin() as session:
            repo = SupportRepository(session)
            repo.require_role(actor, "support_agent" if staff else "user")
            ticket = repo.ticket(ticket_id, actor, staff=staff, lock=True)
            allowed = ("in_progress",) if staff else ("open", "in_progress")
            if ticket.status not in allowed or (staff and ticket.assigned_staff_user_id != actor):
                raise SupportTicketStateConflictError()
            session.add(
                SupportTicketMessage(
                    ticket_id=ticket_id,
                    author_user_id=actor,
                    body=body,
                    created_at=datetime.now(UTC),
                )
            )
            self._event(session, ticket_id, actor, "message_sent", correlation)

    def transition(
        self, actor: int, ticket_id: int, summary: str, correlation: str, *, escalate: bool
    ) -> None:
        with self.factory.begin() as session:
            repo = SupportRepository(session)
            repo.require_role(actor, "support_agent")
            ticket = repo.ticket(ticket_id, actor, staff=True, lock=True)
            if ticket.status != "in_progress" or ticket.assigned_staff_user_id != actor:
                raise SupportTicketStateConflictError()
            status = "escalated" if escalate else "resolved"
            values: dict[str, object] = {"status": status}
            if escalate:
                values["originating_support_agent_id"] = actor
            else:
                values["resolution_summary"] = summary
            session.execute(
                update(SupportTicket).where(SupportTicket.id == ticket_id).values(**values)
            )
            self._event(
                session,
                ticket_id,
                actor,
                f"ticket_{status}",
                correlation,
                previous="in_progress",
                status=status,
                reason=summary if escalate else None,
            )

"""Support application unit of work: authorization, mutation and audit commit together."""

from base64 import b64encode
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from finance_ai.audit.service import SecurityAuditWriter
from finance_ai.core.errors import SupportTicketNotFoundError, SupportTicketStateConflictError
from finance_ai.db.models.support import (
    SupportAttachment,
    SupportTicket,
    SupportTicketActivity,
    SupportTicketMessage,
)
from finance_ai.support.attachments import decode_attachment
from finance_ai.support.repository import SupportRepository
from finance_ai.support.schemas import (
    AgentTicketCreate,
    AttachmentDownload,
    AttachmentInput,
    AttachmentSummary,
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
        status: str | None = None,
    ) -> None:
        session.add(
            SupportTicketActivity(
                ticket_id=ticket_id,
                actor_user_id=actor,
                event_type=event,
                new_status=status,
                created_at=datetime.now(UTC),
            )
        )
        self._audit_event(session, ticket_id, actor, event, correlation)

    def _audit_event(
        self, session: Session, ticket_id: int, actor: int, event: str, correlation: str
    ) -> None:
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

    @staticmethod
    def _attach(
        session: Session, ticket_id: int, file: AttachmentInput, message_id: int | None = None
    ) -> None:
        content = decode_attachment(file)
        session.add(
            SupportAttachment(
                ticket_id=ticket_id,
                message_id=message_id,
                name=file.name,
                size=len(content),
                content=content,
            )
        )

    def download(
        self, actor: int, ticket_id: int, attachment_id: int, *, staff: bool
    ) -> AttachmentDownload:
        with self.factory.begin() as session:
            repo = SupportRepository(session)
            repo.require_role(actor, "support_agent" if staff else "user")
            repo.ticket(ticket_id, actor, staff=staff)
            content = session.scalar(
                select(SupportAttachment.content).where(
                    SupportAttachment.ticket_id == ticket_id, SupportAttachment.id == attachment_id
                )
            )
            if content is None:
                raise SupportTicketNotFoundError()
            return AttachmentDownload(content=b64encode(content).decode("ascii"))

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
            if data.attachment is not None:
                self._attach(session, ticket.id, data.attachment)
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
            files = SupportAttachment.__table__
            rows = session.execute(
                select(*(files.c[name] for name in AttachmentSummary.model_fields))
                .where(files.c.ticket_id == ticket_id)
                .order_by(files.c.id)
            ).mappings()
            attachments = [AttachmentSummary.model_validate(row) for row in rows]
            if staff:
                return StaffConversation(
                    ticket=ticket,
                    messages=messages,
                    attachments=attachments,
                    customer=repo.customer(str(repo.customer_id(ticket_id))),
                    history=repo.history(ticket_id),
                )
            return CustomerConversation(ticket=ticket, messages=messages, attachments=attachments)

    def message(
        self,
        actor: int,
        ticket_id: int,
        body: str,
        correlation: str,
        *,
        staff: bool,
        attachment: AttachmentInput | None = None,
    ) -> None:
        with self.factory.begin() as session:
            repo = SupportRepository(session)
            repo.require_role(actor, "support_agent" if staff else "user")
            ticket = repo.ticket(ticket_id, actor, staff=staff, lock=True)
            allowed = ("in_progress",) if staff else ("open", "in_progress")
            if ticket.status not in allowed or (staff and ticket.assigned_staff_user_id != actor):
                raise SupportTicketStateConflictError()
            message = SupportTicketMessage(
                ticket_id=ticket_id, author_user_id=actor, body=body, created_at=datetime.now(UTC)
            )
            session.add(message)
            if attachment is not None:
                session.flush()
                self._attach(session, ticket_id, attachment, message.id)
            self._event(session, ticket_id, actor, "message_sent", correlation)

    def transition(
        self, actor: int, ticket_id: int, summary: str, correlation: str, *, escalate: bool
    ) -> None:
        with self.factory.begin() as session:
            repo = SupportRepository(session)
            repo.require_role(actor, "support_agent")
            # Preserve the existing 404 for tickets the agent cannot see. Lock the
            # same row as message() so a terminal transition serializes with replies.
            repo.ticket(ticket_id, actor, staff=True, lock=True)
            statement = (
                text("CALL sp_support_escalate_ticket(:actor, :ticket_id, :summary, NULL)")
                if escalate
                else text("CALL sp_support_resolve_ticket(:actor, :ticket_id, :summary, NULL)")
            )
            applied = session.execute(
                statement, {"actor": actor, "ticket_id": ticket_id, "summary": summary}
            ).scalar_one()
            if not applied:
                raise SupportTicketStateConflictError()

            # The procedure already inserted ticket history. Add the correlated
            # security event in this transaction so an audit failure rolls back both.
            event = "ticket_escalated" if escalate else "ticket_resolved"
            self._audit_event(session, ticket_id, actor, event, correlation)

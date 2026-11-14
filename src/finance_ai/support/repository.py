"""Allowlisted projections; persistence is staged in the caller's transaction."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_ai.core.errors import StaffAccessDeniedError, SupportTicketNotFoundError
from finance_ai.db.models.identity import AppUser, Role
from finance_ai.db.models.support import SupportTicket, SupportTicketActivity, SupportTicketMessage
from finance_ai.support.schemas import (
    CustomerMetadata,
    CustomerTicket,
    TicketHistory,
    TicketMessage,
)


class SupportRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def require_role(self, user_id: int, role: str) -> None:
        permitted = self.session.scalar(
            select(AppUser.id)
            .join(Role, Role.id == AppUser.role_id)
            .where(AppUser.id == user_id, AppUser.is_active.is_(True), Role.name == role)
        )
        if permitted is None:
            raise StaffAccessDeniedError()

    def customer(self, identifier: str) -> CustomerMetadata:
        # No LIKE, prefix matching, arbitrary lists or private account relationships.
        condition = AppUser.email == identifier.strip().lower()
        if (
            identifier.isascii()
            and identifier.isdecimal()
            and len(identifier) <= 19
            and int(identifier) <= 9223372036854775807
        ):
            condition = AppUser.id == int(identifier)
        row = (
            self.session.execute(
                select(AppUser.id, AppUser.email, AppUser.is_active)
                .join(Role, Role.id == AppUser.role_id)
                .where(condition, Role.name == "user")
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise SupportTicketNotFoundError()
        return CustomerMetadata.model_validate(row)

    def ticket(
        self, ticket_id: int, user_id: int, *, staff: bool, lock: bool = False
    ) -> CustomerTicket:
        table = SupportTicket.__table__
        authorized = (
            (table.c.assigned_staff_user_id == user_id)
            if staff
            else (table.c.customer_user_id == user_id)
        )
        if staff:
            authorized = authorized | (
                (table.c.originating_support_agent_id == user_id) & (table.c.status == "escalated")
            )
        statement = select(*(table.c[name] for name in CustomerTicket.model_fields)).where(
            table.c.id == ticket_id, authorized
        )
        if lock:
            statement = statement.with_for_update()
        row = self.session.execute(statement).mappings().one_or_none()
        if row is None:
            raise SupportTicketNotFoundError()
        return CustomerTicket.model_validate(row)

    def customer_id(self, ticket_id: int) -> int:
        value = self.session.scalar(
            select(SupportTicket.customer_user_id).where(SupportTicket.id == ticket_id)
        )
        assert value is not None
        return value

    def messages(self, ticket_id: int) -> list[TicketMessage]:
        table = SupportTicketMessage.__table__
        rows = self.session.execute(
            select(*(table.c[name] for name in TicketMessage.model_fields))
            .where(table.c.ticket_id == ticket_id)
            .order_by(table.c.created_at, table.c.id)
        ).mappings()
        return [TicketMessage.model_validate(row) for row in rows]

    def history(self, ticket_id: int) -> list[TicketHistory]:
        table = SupportTicketActivity.__table__
        rows = self.session.execute(
            select(*(table.c[name] for name in TicketHistory.model_fields))
            .where(table.c.ticket_id == ticket_id)
            .order_by(table.c.created_at, table.c.id)
        ).mappings()
        return [TicketHistory.model_validate(row) for row in rows]

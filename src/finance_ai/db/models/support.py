from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Identity, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base, BigIntPK
from finance_ai.db.models.identity import AppUser


class SupportTicket(Base):
    __tablename__ = "support_ticket"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'in_progress', 'escalated', 'resolved')",
            name="approved_status",
        ),
        CheckConstraint("length(trim(subject)) > 0", name="subject_nonempty"),
        CheckConstraint("length(trim(category)) > 0", name="category_nonempty"),
        CheckConstraint("length(trim(description)) > 0", name="description_nonempty"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    customer_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="open")
    assigned_staff_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    originating_support_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    resolution_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped[AppUser] = relationship(foreign_keys=[customer_user_id], lazy="joined")
    creator: Mapped[AppUser] = relationship(foreign_keys=[created_by_user_id], lazy="joined")
    assignee: Mapped[AppUser | None] = relationship(
        foreign_keys=[assigned_staff_user_id], lazy="joined"
    )
    originating_agent: Mapped[AppUser | None] = relationship(
        foreign_keys=[originating_support_agent_id], lazy="joined"
    )
    messages: Mapped[list[SupportTicketMessage]] = relationship(back_populates="ticket")
    activities: Mapped[list[SupportTicketActivity]] = relationship(
        back_populates="ticket",
        order_by="SupportTicketActivity.created_at, SupportTicketActivity.id",
    )


class SupportTicketMessage(Base):
    __tablename__ = "support_ticket_message"
    __table_args__ = (CheckConstraint("length(trim(body)) > 0", name="body_nonempty"),)

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("support_ticket.id", ondelete="RESTRICT"), nullable=False
    )
    author_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ticket: Mapped[SupportTicket] = relationship(back_populates="messages")
    author: Mapped[AppUser] = relationship(lazy="joined")


class SupportTicketActivity(Base):
    __tablename__ = "support_ticket_activity"
    __table_args__ = (
        CheckConstraint(
            "previous_status IS NULL OR previous_status IN "
            "('open', 'in_progress', 'escalated', 'resolved')",
            name="previous_status_approved",
        ),
        CheckConstraint(
            "new_status IS NULL OR new_status IN ('open', 'in_progress', 'escalated', 'resolved')",
            name="new_status_approved",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("support_ticket.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(20))
    new_status: Mapped[str | None] = mapped_column(String(20))
    previous_assignee_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    new_assignee_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    internal_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ticket: Mapped[SupportTicket] = relationship(back_populates="activities")
    actor: Mapped[AppUser] = relationship(foreign_keys=[actor_user_id], lazy="joined")


Index("ix_support_ticket_status_created", SupportTicket.status, SupportTicket.created_at)
Index(
    "ix_support_ticket_assignee_status",
    SupportTicket.assigned_staff_user_id,
    SupportTicket.status,
)
Index(
    "ix_support_ticket_customer_created",
    SupportTicket.customer_user_id,
    SupportTicket.created_at,
    SupportTicket.id,
)
Index(
    "ix_support_ticket_activity_history",
    SupportTicketActivity.ticket_id,
    SupportTicketActivity.created_at,
    SupportTicketActivity.id,
)
Index(
    "ix_support_ticket_message_history",
    SupportTicketMessage.ticket_id,
    SupportTicketMessage.created_at,
    SupportTicketMessage.id,
)

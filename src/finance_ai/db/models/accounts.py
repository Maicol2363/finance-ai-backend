from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finance_ai.db.base import Base
from finance_ai.db.models.identity import AppUser
from finance_ai.db.models.reference import Currency


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (
        CheckConstraint(
            "name = btrim(name) AND char_length(name) BETWEEN 1 AND 100",
            name="name_trimmed_non_empty",
        ),
        CheckConstraint(
            "type IN ('cash', 'bank', 'digital_wallet', 'savings', 'credit_card', 'other')",
            name="type_supported",
        ),
        UniqueConstraint("id", "user_id", name="uq_account_id_user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_type: Mapped[str] = mapped_column("type", String(32), nullable=False)
    currency_id: Mapped[int] = mapped_column(
        ForeignKey("currency.id", ondelete="RESTRICT"), nullable=False
    )
    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, server_default=text("0.00")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[AppUser] = relationship(lazy="raise")
    currency: Mapped[Currency] = relationship(lazy="joined")


Index(
    "uq_account_active_name",
    Account.user_id,
    func.lower(Account.name),
    unique=True,
    postgresql_where=Account.is_active.is_(True),
)
Index(
    "ix_account_user_active_name",
    Account.user_id,
    Account.is_active,
    func.lower(Account.name),
    Account.id,
)
Index("ix_account_user_created", Account.user_id, Account.created_at.desc(), Account.id.desc())
